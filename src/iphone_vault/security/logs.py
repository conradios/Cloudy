"""Registro persistente en disco, sin datos personales.

Se guarda en la carpeta de configuración de la app (Windows: %APPDATA%\\iPhoneVault\\logs), rota
a los 1 MB y conserva 5 archivos. Antes de escribir cada línea se enmascaran:
  - correos (Apple ID): juan.perez@icloud.com → j***@icloud.com
  - códigos de 6 dígitos sueltos (2FA)
  - tokens/cookies largos
Nunca se escriben contraseñas: la app no las registra en ningún lado.
"""
from __future__ import annotations

import logging
import re
import zipfile
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config import config_dir

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CODE = re.compile(r"(?<![\w/.-])\d{6}(?![\w/.-])")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

LOGGER_NAME = "iphone_vault"


def mask(text: str) -> str:
    text = _EMAIL.sub(r"\1***@\2", text)
    text = _TOKEN.sub("[token oculto]", text)
    return _CODE.sub("[código oculto]", text)


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(str(record.getMessage()))
        record.args = ()
        return True


def logs_dir() -> Path:
    d = config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_logger() -> logging.Logger:
    lg = logging.getLogger(LOGGER_NAME)
    if not lg.handlers:
        h = RotatingFileHandler(logs_dir() / "iphone-vault.log", maxBytes=1_000_000, backupCount=5,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        h.addFilter(_MaskFilter())
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


def log_line(text: str) -> None:
    level = logging.INFO
    up = text.upper()
    if up.startswith(("ERROR", "FALLÓ", "FALLO")):
        level = logging.ERROR
    elif up.startswith(("AVISO", "INTERRUMPIDO", "ATENCIÓN")):
        level = logging.WARNING
    try:
        get_logger().log(level, text)
    except Exception:  # noqa: BLE001  (el registro nunca debe frenar la app)
        pass


def export_logs(target: Path) -> Path:
    """Empaqueta los registros (ya enmascarados) en un .zip para compartirlos si hace falta ayuda."""
    target = Path(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(logs_dir().glob("iphone-vault.log*")):
            z.write(f, f.name)
        z.writestr("LEEME.txt", f"Registros de Cloudy exportados el {datetime.now():%Y-%m-%d %H:%M}.\n"
                               "Los correos, códigos y tokens están enmascarados. No contienen fotos.\n")
    return target
