"""«Salud de tu copia»: verificación completa de integridad de lo ya copiado.

Para cada archivo registrado en el catálogo:
  - que exista,
  - que tenga exactamente el tamaño registrado,
  - que su SHA-256 actual sea idéntico al calculado al copiarlo (detecta archivos dañados por el
    disco, «bit rot», o modificados por otro programa),
  - (opcional) que su estructura interna siga siendo válida.
Además informa restos de copias a medias (.part) y archivos de la carpeta que la app no conoce.

«Reparar» quita del catálogo los archivos faltantes o dañados: la próxima «Copiar ahora» los vuelve a
traer del iPhone o de iCloud (el dañado se renombra a «.danado» para no perderlo hasta confirmar).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..catalog.manifest import STATUS_FAILED
from .compat import COMPAT_DIR
from .validate import validate_file

CHUNK = 4 * 1024 * 1024


@dataclass
class VerifyProblem:
    local_path: str
    remote_path: str
    kind: str          # "falta" | "dañado" | "tamaño"
    detail: str = ""


@dataclass
class VerifyReport:
    checked: int = 0
    ok: int = 0
    bytes_checked: int = 0
    problems: list[VerifyProblem] = field(default_factory=list)
    structure_warnings: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)       # .part de copias interrumpidas
    unknown_files: int = 0                                   # archivos en la carpeta que la app no copió
    cancelled: bool = False
    seconds: float = 0.0
    finished_at: str = ""

    @property
    def healthy(self) -> bool:
        return not self.problems and not self.cancelled


def _sha(p: Path, cancel: threading.Event, on_bytes: Callable[[int], None]) -> Optional[str]:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            if cancel.is_set():
                return None
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
            on_bytes(len(b))
    return h.hexdigest()


def verify_library(manifest, progress: Callable[[int, int, int, int, str], None],
                   cancel: Optional[threading.Event] = None, structure: bool = False) -> VerifyReport:
    """progress(archivo_i, total_archivos, bytes_hechos, bytes_totales, nombre)."""
    cancel = cancel or threading.Event()
    rep = VerifyReport()
    t0 = time.monotonic()
    root = manifest.root
    files = manifest.all_files()
    total_bytes = sum(f["size"] for f in files)
    done_bytes = 0
    last = [0.0]

    def tick(n, i, name):
        nonlocal done_bytes
        done_bytes += n
        now = time.monotonic()
        if now - last[0] > 0.2:
            last[0] = now
            progress(i, len(files), done_bytes, total_bytes, name)

    for i, f in enumerate(files, start=1):
        if cancel.is_set():
            rep.cancelled = True
            break
        rel, p = f["local_path"], root / f["local_path"]
        progress(i, len(files), done_bytes, total_bytes, Path(rel).name)
        rep.checked += 1
        if not p.exists():
            rep.problems.append(VerifyProblem(rel, f["remote_path"], "falta", "el archivo no está en la carpeta"))
            done_bytes += f["size"]
            continue
        size = p.stat().st_size
        if size != f["size"]:
            rep.problems.append(VerifyProblem(rel, f["remote_path"], "tamaño",
                                              f"tiene {size} bytes y debería tener {f['size']}"))
            done_bytes += f["size"]
            continue
        digest = _sha(p, cancel, lambda n, i=i, name=Path(rel).name: tick(n, i, name))
        if digest is None:
            rep.cancelled = True
            break
        rep.bytes_checked += size
        if digest != f["sha256"]:
            rep.problems.append(VerifyProblem(rel, f["remote_path"], "dañado",
                                              "el contenido cambió desde que se copió (SHA-256 distinto)"))
            continue
        if structure:
            v = validate_file(p)
            if v.failed:
                rep.structure_warnings.append(f"{rel}: {v.detail}")
        rep.ok += 1

    if not rep.cancelled:
        known = {str((root / f["local_path"]).resolve()) for f in files}
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".vault", COMPAT_DIR)]
            for n in names:
                full = Path(dirpath) / n
                if n.endswith(".part"):
                    rep.leftovers.append(str(full.relative_to(root)))
                elif str(full.resolve()) not in known and not n.endswith(".json"):
                    rep.unknown_files += 1

    rep.seconds = time.monotonic() - t0
    rep.finished_at = datetime.now().isoformat(timespec="seconds")
    if not rep.cancelled:
        manifest.set_meta("verify_last_at", rep.finished_at)
        manifest.set_meta("verify_last_checked", str(rep.checked))
        manifest.set_meta("verify_last_problems", str(len(rep.problems)))
        try:
            manifest.backup()
        except Exception:  # noqa: BLE001
            pass
    return rep


def repair(manifest, problems: list[VerifyProblem]) -> int:
    """Saca del catálogo los archivos faltantes/dañados para que la próxima copia los vuelva a traer."""
    n = 0
    root = manifest.root
    for pr in problems:
        p = root / pr.local_path
        if p.exists():
            try:
                os.replace(p, p.with_name(p.name + ".danado"))
            except OSError:
                pass
        rec = manifest.file_record(pr.remote_path)
        with manifest._lock:  # noqa: SLF001
            manifest._con.execute("DELETE FROM files WHERE remote_path=?", (pr.remote_path,))  # noqa: SLF001
            if rec:
                manifest._con.execute("UPDATE items SET status=?, reason=? WHERE key=?",  # noqa: SLF001
                                      (STATUS_FAILED, "dañado o faltante: se vuelve a copiar", rec["item_key"]))
            manifest._con.commit()  # noqa: SLF001
        n += 1
    return n


def last_verification(manifest) -> dict:
    at = manifest.get_meta("verify_last_at")
    return {
        "at": datetime.fromisoformat(at) if at else None,
        "checked": int(manifest.get_meta("verify_last_checked", "0") or 0),
        "problems": int(manifest.get_meta("verify_last_problems", "0") or 0),
    }
