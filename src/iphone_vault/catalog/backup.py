"""Respaldo y recuperación del catálogo (.vault/manifest.db).

El catálogo es lo que le permite a la app saber qué ya copió. Si se dañara (corte de luz en el peor
momento, disco con sectores malos), sin él la app tendría que volver a revisar todo. Por eso:
  - se respalda solo (API de backup de SQLite, consistente aunque esté en uso) después de cada
    copia y de cada verificación, y se guardan los últimos 7 respaldos;
  - al abrir, se comprueba (PRAGMA quick_check); si está dañado se restaura el respaldo más nuevo.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

KEEP = 7


def backups_dir(vault_dir: Path) -> Path:
    d = vault_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_now(con: sqlite3.Connection, vault_dir: Path) -> Path:
    target = backups_dir(vault_dir) / f"manifest-{datetime.now():%Y%m%d-%H%M%S}.db"
    dst = sqlite3.connect(str(target))
    try:
        con.backup(dst)
    finally:
        dst.close()
    for old in sorted(backups_dir(vault_dir).glob("manifest-*.db"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def list_backups(vault_dir: Path) -> list[Path]:
    return sorted(backups_dir(vault_dir).glob("manifest-*.db"), reverse=True)


def is_healthy(db: Path) -> bool:
    if not db.exists():
        return True
    try:
        con = sqlite3.connect(str(db))
        try:
            return con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def restore_latest(vault_dir: Path, db: Path) -> Optional[Path]:
    """Reemplaza un catálogo dañado por el respaldo sano más nuevo. Guarda el dañado aparte."""
    for b in list_backups(vault_dir):
        if is_healthy(b):
            broken = db.with_name(f"manifest-danado-{datetime.now():%Y%m%d-%H%M%S}.db")
            if db.exists():
                shutil.move(str(db), str(broken))
            for suffix in ("-wal", "-shm"):
                side = Path(str(db) + suffix)
                if side.exists():
                    side.unlink()
            shutil.copy2(b, db)
            return b
    return None
