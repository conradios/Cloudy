"""Catálogo local (SQLite) dentro de la carpeta destino: <destino>/.vault/manifest.db

Guarda qué archivo del iPhone ya se copió, dónde quedó y su SHA-256, y la lista de
elementos PENDIENTES (originales que estaban solo en iCloud o en versión optimizada).
Gracias a esto la segunda vez solo se copia lo nuevo, y «Descargar pendientes»
baja únicamente esos elementos.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import MediaItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key            TEXT PRIMARY KEY,
    primary_path   TEXT NOT NULL,
    kind           TEXT,
    capture_date   TEXT,
    status         TEXT NOT NULL,          -- done | pending | failed
    reason         TEXT,
    expected_size  INTEGER,
    first_seen     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS files (
    remote_path    TEXT PRIMARY KEY,
    item_key       TEXT NOT NULL,
    role           TEXT,
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    local_path     TEXT NOT NULL UNIQUE,   -- relativo a la carpeta destino
    copied_at      TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS mirrors (
    mirror_root    TEXT NOT NULL,
    local_path     TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    copied_at      TEXT,
    PRIMARY KEY (mirror_root, local_path)
);
CREATE TABLE IF NOT EXISTS deletions (
    item_key       TEXT PRIMARY KEY,
    name           TEXT,
    size           INTEGER,
    deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_files_item ON files(item_key);
"""

MIGRATIONS = [
    ("items", "fingerprint", "TEXT"),
    ("items", "source", "TEXT"),
    ("files", "validation", "TEXT"),
]

STATUS_DONE = "done"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_EXCLUDED = "excluded"     # el usuario dijo «no copiar este»
STATUS_RESOLVED = "resolved"     # pendiente que se consiguió por otra fuente (ej. iCloud)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DestinationInUseError(RuntimeError):
    pass


# Bloqueo entre procesos: dos ventanas de la app copiando a la misma carpeta podrían pisarse.
_held: dict[str, list] = {}        # carpeta -> [archivo, cantidad de usos en este proceso]
_held_lock = threading.Lock()


def _acquire_lock(vault_dir: Path) -> None:
    key = str(vault_dir.resolve())
    with _held_lock:
        if key in _held:
            _held[key][1] += 1
            return
        fh = open(vault_dir / "lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise DestinationInUseError("Esta carpeta destino ya está siendo usada por otra ventana de Cloudy. "
                                        "Cerrala (o esperá a que termine) y volvé a intentar.")
        _held[key] = [fh, 1]


def _release_lock(vault_dir: Path) -> None:
    key = str(vault_dir.resolve())
    with _held_lock:
        entry = _held.get(key)
        if not entry:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            try:
                if os.name == "nt":
                    import msvcrt
                    entry[0].seek(0)
                    msvcrt.locking(entry[0].fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            entry[0].close()
            del _held[key]


class Manifest:
    def __init__(self, destination: Path):
        self.root = Path(destination)
        self.dir = self.root / ".vault"
        self.dir.mkdir(parents=True, exist_ok=True)
        _acquire_lock(self.dir)
        self._closed = False
        self.path = self.dir / "manifest.db"
        self.restored_from = None
        from .backup import is_healthy, restore_latest
        if not is_healthy(self.path):
            self.restored_from = restore_latest(self.dir, self.path)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self._con.execute("CREATE INDEX IF NOT EXISTS idx_items_fp ON items(fingerprint)")
        # v0.2 → v0.3: la huella ahora incluye el tamaño del original
        self._con.execute("UPDATE items SET fingerprint = fingerprint || '|' || expected_size "
                          "WHERE fingerprint IS NOT NULL AND fingerprint NOT LIKE '%|%|%' AND expected_size IS NOT NULL")
        self._con.commit()

    def backup(self):
        """Respaldo consistente del catálogo en .vault/backups (se guardan los últimos 7)."""
        from .backup import backup_now
        with self._lock:
            return backup_now(self._con, self.dir)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._con.close()
        _release_lock(self.dir)

    # ---------------------------------------------------------------- archivos
    def file_record(self, remote_path: str) -> Optional[dict]:
        with self._lock:
            row = self._con.execute(
                "SELECT remote_path, item_key, role, size, sha256, local_path, validation FROM files WHERE remote_path=?",
                (remote_path,),
            ).fetchone()
        if not row:
            return None
        return dict(zip(("remote_path", "item_key", "role", "size", "sha256", "local_path", "validation"), row))

    def is_file_done(self, remote_path: str, size: int) -> bool:
        rec = self.file_record(remote_path)
        return bool(rec and rec["size"] == size and (self.root / rec["local_path"]).exists())

    def local_path_owner(self, local_rel: str) -> Optional[str]:
        with self._lock:
            row = self._con.execute("SELECT remote_path FROM files WHERE local_path=?", (local_rel,)).fetchone()
        return row[0] if row else None

    def record_file(self, item_key: str, remote_path: str, role: str, size: int, sha256: str, local_rel: str,
                    validation: str = ""):
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO files(remote_path,item_key,role,size,sha256,local_path,copied_at,validation) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (remote_path, item_key, role, size, sha256, local_rel, _now(), validation),
            )
            self._con.commit()

    def files_of_item(self, item_key: str) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT remote_path, role, size, sha256, local_path, validation FROM files WHERE item_key=?",
                (item_key,)).fetchall()
        return [dict(zip(("remote_path", "role", "size", "sha256", "local_path", "validation"), r)) for r in rows]

    # ------------------------------------------------------------ espejos
    def mirror_has(self, mirror_root: str, local_rel: str, sha256: str) -> bool:
        with self._lock:
            row = self._con.execute("SELECT sha256 FROM mirrors WHERE mirror_root=? AND local_path=?",
                                    (mirror_root, local_rel)).fetchone()
        return bool(row and row[0] == sha256 and (Path(mirror_root) / local_rel).exists())

    def record_mirror(self, mirror_root: str, local_rel: str, sha256: str):
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO mirrors VALUES (?,?,?,?)", (mirror_root, local_rel, sha256, _now()))
            self._con.commit()

    def files_map(self) -> dict[str, tuple[int, str]]:
        """remote_path → (tamaño, ruta local). Para clasificar miles de elementos rápido."""
        with self._lock:
            return {r[0]: (r[1], r[2]) for r in self._con.execute("SELECT remote_path, size, local_path FROM files")}

    def done_key_by_fingerprint(self) -> dict[str, str]:
        with self._lock:
            return {fp: k for fp, k in self._con.execute(
                "SELECT fingerprint, key FROM items WHERE status=? AND fingerprint IS NOT NULL", (STATUS_DONE,))}

    def all_files(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute("SELECT remote_path, item_key, size, sha256, local_path FROM files").fetchall()
        return [dict(zip(("remote_path", "item_key", "size", "sha256", "local_path"), r)) for r in rows]

    # ------------------------------------------------------------ meta / borrados
    def get_meta(self, k: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_meta(self, k: str, v: str):
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            self._con.commit()

    def record_deletion(self, item_key: str, name: str, size: int):
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO deletions VALUES (?,?,?,?)", (item_key, name, size, _now()))
            self._con.commit()

    def deleted_keys(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._con.execute("SELECT item_key FROM deletions")}

    def deletion_totals(self) -> tuple[int, int]:
        with self._lock:
            n, b = self._con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM deletions").fetchone()
        return n, b

    # ---------------------------------------------------------------- elementos
    def item_status(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._con.execute("SELECT status FROM items WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _upsert_item(self, item: MediaItem, status: str, reason: Optional[str]):
        with self._lock:
            self._con.execute(
                """INSERT INTO items(key,primary_path,kind,capture_date,status,reason,expected_size,first_seen,
                                     updated_at,fingerprint,source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET primary_path=excluded.primary_path, kind=excluded.kind,
                     capture_date=excluded.capture_date, status=excluded.status, reason=excluded.reason,
                     expected_size=excluded.expected_size, updated_at=excluded.updated_at,
                     fingerprint=excluded.fingerprint, source=excluded.source""",
                (item.key, item.primary_path, item.kind,
                 item.capture_date.isoformat() if item.capture_date else None,
                 status, reason, item.expected_size, _now(), _now(), item.fingerprint, item.source),
            )
            self._con.commit()

    def done_fingerprints(self) -> set[str]:
        """Huellas de elementos ya copiados (de cualquier fuente): evita duplicar iPhone vs iCloud."""
        with self._lock:
            return {r[0] for r in self._con.execute(
                "SELECT fingerprint FROM items WHERE status=? AND fingerprint IS NOT NULL", (STATUS_DONE,))}

    def pending_fingerprints(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._con.execute(
                "SELECT fingerprint FROM items WHERE status=? AND fingerprint IS NOT NULL", (STATUS_PENDING,))}

    def resolve_pending_by_fingerprint(self, fingerprint: Optional[str], via: str):
        """Cuando un pendiente se consigue por otra fuente (ej. iCloud), se marca resuelto."""
        if not fingerprint:
            return
        with self._lock:
            self._con.execute("UPDATE items SET status=?, reason=?, updated_at=? WHERE fingerprint=? AND status=?",
                              (STATUS_RESOLVED, f"resuelto vía {via}", _now(), fingerprint, STATUS_PENDING))
            self._con.commit()

    def set_excluded(self, item: MediaItem, excluded: bool):
        if excluded:
            self._upsert_item(item, STATUS_EXCLUDED, "excluido por el usuario")
        else:
            with self._lock:
                self._con.execute("DELETE FROM items WHERE key=? AND status=?", (item.key, STATUS_EXCLUDED))
                self._con.commit()

    def remove_pending(self, key: str):
        with self._lock:
            self._con.execute("DELETE FROM items WHERE key=? AND status=?", (key, STATUS_PENDING))
            self._con.commit()

    def mark_pending(self, item: MediaItem, reason: str, done_fps: Optional[set] = None):
        """Registra un elemento cuyo original no está disponible. No pisa uno ya copiado ni excluido."""
        if self.item_status(item.key) in (STATUS_DONE, STATUS_EXCLUDED, STATUS_RESOLVED):
            return
        if done_fps is None:
            done_fps = self.done_fingerprints()
        if item.fingerprint and item.fingerprint in done_fps:
            return   # ya se consiguió por otra fuente
        self._upsert_item(item, STATUS_PENDING, reason)

    def mark_done(self, item: MediaItem):
        self._upsert_item(item, STATUS_DONE, None)

    def mark_failed(self, item: MediaItem, error: str):
        self._upsert_item(item, STATUS_FAILED, error[:500])

    def keys_with_status(self, *statuses: str) -> set[str]:
        q = ",".join("?" * len(statuses))
        with self._lock:
            # solo se arma la lista de «?»; los valores van como parámetros
            return {r[0] for r in self._con.execute(f"SELECT key FROM items WHERE status IN ({q})",  # nosec B608
                                                    statuses)}

    def pending_rows(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT key, primary_path, kind, capture_date, reason, expected_size, first_seen "
                "FROM items WHERE status=? ORDER BY capture_date DESC", (STATUS_PENDING,)
            ).fetchall()
        cols = ("key", "primary_path", "kind", "capture_date", "reason", "expected_size", "first_seen")
        return [dict(zip(cols, r)) for r in rows]

    def failed_rows(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute("SELECT key, primary_path, reason FROM items WHERE status=?",
                                     (STATUS_FAILED,)).fetchall()
        return [dict(zip(("key", "primary_path", "reason"), r)) for r in rows]

    def counts(self) -> dict:
        with self._lock:
            rows = self._con.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall()
            files = self._con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
        out = {s: n for s, n in rows}
        out["files"], out["bytes"] = files
        return out

    def export_pending_csv(self, path: Path) -> int:
        rows = self.pending_rows()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Archivo", "Tipo", "Fecha", "Motivo", "Tamaño original (bytes)", "Detectado", "ID"])
            for r in rows:
                w.writerow([r["primary_path"], r["kind"], r["capture_date"] or "", r["reason"] or "",
                            r["expected_size"] or "", r["first_seen"], r["key"]])
        return len(rows)
