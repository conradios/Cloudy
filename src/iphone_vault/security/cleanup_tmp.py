"""Carpeta de trabajo temporal propia (para la copia de Photos.sqlite durante el escaneo).

Photos.sqlite contiene metadatos sensibles de TODA tu fototeca (fechas, ubicaciones GPS, álbumes).
Por eso la copia temporal no va a la carpeta temporal general del sistema, sino a una carpeta
propia y privada de la app, que se borra al terminar cada escaneo y además se vacía cada vez
que la app arranca (por si el proceso se cortó de golpe: corte de luz, kill, cuelgue).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..config import config_dir


def work_tmp() -> Path:
    d = config_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def new_tmpdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=work_tmp()))


def purge_stale() -> int:
    """Borra cualquier resto de ejecuciones anteriores. Devuelve cuántos elementos eliminó."""
    d = config_dir() / "tmp"
    if not d.exists():
        return 0
    n = sum(1 for _ in d.rglob("*"))
    shutil.rmtree(d, ignore_errors=True)
    return n
