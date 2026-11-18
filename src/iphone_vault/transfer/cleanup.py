"""«Liberar espacio»: manda a «Eliminado recientemente» de iCloud solo lo pesado que YA está a salvo en la compu.

Reglas de seguridad (todas obligatorias):
  1. Solo elementos de iCloud cuya copia local existe y figura como copiada y verificada en el catálogo
     (por iCloud o por cable: se reconoce la misma foto por nombre original + segundo de captura).
  2. El tamaño del archivo local tiene que ser EXACTAMENTE el del original de iCloud
     (así nunca se borra algo de lo que solo tenés una versión optimizada).
  3. Justo antes de borrar se vuelve a calcular el SHA-256 de la copia local y se compara con el
     registrado al copiarla. Si no coincide, ese elemento NO se borra.
  4. Se borra por tandas y solo a «Eliminado recientemente»: Apple lo conserva 30 días y se puede recuperar.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..catalog.manifest import STATUS_DONE, Manifest
from ..config import CleanupRules
from ..models import ROLE_ORIGINAL, MediaItem

CHUNK = 4 * 1024 * 1024


@dataclass
class CleanupCandidate:
    item: MediaItem
    local_files: list[dict]       # registros del catálogo (local_path, size, sha256)

    @property
    def size(self) -> int:
        return self.item.best_size


@dataclass
class CleanupReport:
    deleted: int = 0
    freed_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def _done_keys_by_fingerprint(manifest: Manifest) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with manifest._lock:  # noqa: SLF001
        rows = manifest._con.execute(  # noqa: SLF001
            "SELECT fingerprint, key FROM items WHERE status=? AND fingerprint IS NOT NULL", (STATUS_DONE,)).fetchall()
    for fp, key in rows:
        out.setdefault(fp, []).append(key)
    return out


def find_candidates(icloud_items: list[MediaItem], manifest: Manifest, rules: CleanupRules) -> list[CleanupCandidate]:
    by_fp = _done_keys_by_fingerprint(manifest)
    deleted = manifest.deleted_keys()
    limit_date = datetime.now().astimezone() - timedelta(days=max(0, rules.older_than_days))
    out = []
    for it in icloud_items:
        if it.source != "icloud" or it.key in deleted:
            continue
        if rules.only_videos and it.kind != "video":
            continue
        if it.best_size < rules.min_size_mb * 1024 * 1024:
            continue
        if rules.older_than_days and (it.capture_date is None or it.capture_date > limit_date):
            continue
        keys = by_fp.get(it.fingerprint or "", []) if it.fingerprint else []
        if it.key not in keys:
            keys = [it.key] + keys if manifest.item_status(it.key) == STATUS_DONE else keys
        local = None
        for k in keys:
            files = [f for f in manifest.files_of_item(k) if (manifest.root / f["local_path"]).exists()]
            orig = [f for f in files if f["role"] == ROLE_ORIGINAL]
            if orig and orig[0]["size"] == it.files[0].size:   # regla 2: mismo tamaño que el original
                local = files
                break
        if local:
            out.append(CleanupCandidate(it, local))
    out.sort(key=lambda c: c.size, reverse=True)
    return out


def verify_local(manifest: Manifest, cand: CleanupCandidate) -> Optional[str]:
    """Devuelve None si la copia local está intacta; si no, el motivo."""
    for f in cand.local_files:
        p = manifest.root / f["local_path"]
        if not p.exists():
            return f"falta {f['local_path']}"
        if p.stat().st_size != f["size"]:
            return f"tamaño distinto en {f['local_path']}"
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                h.update(block)
        if h.hexdigest() != f["sha256"]:
            return f"SHA-256 distinto en {f['local_path']}"
    return None


def delete_from_icloud(backend, manifest: Manifest, candidates: list[CleanupCandidate], batch_size: int,
                       progress: Callable[[int, int, str], None], cancel: threading.Event) -> CleanupReport:
    rep = CleanupReport()
    todo = candidates[: max(1, batch_size)]
    for i, cand in enumerate(todo, start=1):
        if cancel.is_set():
            rep.cancelled = True
            break
        progress(i, len(todo), cand.item.name)
        problem = verify_local(manifest, cand)
        if problem:
            rep.skipped.append((cand.item.name, problem))
            continue
        try:
            ok = backend.delete_asset(cand.item)
        except Exception as e:  # noqa: BLE001
            rep.skipped.append((cand.item.name, str(e)))
            continue
        if ok:
            manifest.record_deletion(cand.item.key, cand.item.name, cand.size)
            rep.deleted += 1
            rep.freed_bytes += cand.size
        else:
            rep.skipped.append((cand.item.name, "iCloud no confirmó el borrado"))
    return rep
