"""Fecha de captura a partir del NOMBRE del archivo.

Los celulares Android, las cámaras y las apps de mensajería ponen la fecha en el nombre:
  Samsung        20240115_143012.jpg            Screenshot_20240115_143012_Chrome.jpg
  Pixel          PXL_20240115_143012345.jpg     PXL_20240115_143012345.MP.jpg
  Xiaomi/Moto    IMG_20240115_143012.jpg        VID_20240115_143012.mp4
  WhatsApp       IMG-20240115-WA0001.jpg        VID-20240115-WA0003.mp4   (solo día)
  Otros          2024-01-15 14.30.12.jpg        Screenshot_2024-01-15-14-30-12-123_app.png
Es más confiable que la fecha del archivo en el teléfono (que cambia si se movió o copió) y es la misma
venga por MTP o por ADB, así una foto nunca se copia dos veces.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_FULL = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _\-T.]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})(?!\d{3,}[a-z])", re.I)
_DAY = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")


def _mk(y, mo, d, h=12, mi=0, s=0) -> Optional[datetime]:
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if not (1990 <= dt.year <= datetime.now().year + 1):
        return None
    return dt.astimezone()   # hora local del celular → con zona horaria


def date_from_filename(name: str) -> Optional[datetime]:
    stem = name.rsplit("/", 1)[-1]
    for m in _FULL.finditer(stem):
        dt = _mk(*m.groups())
        if dt:
            return dt
    for m in _DAY.finditer(stem):
        dt = _mk(*m.groups())
        if dt:
            return dt
    return None
