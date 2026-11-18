"""Reglas comunes a todos los celulares Android (Samsung, Motorola, Xiaomi, Pixel…) y a las fuentes
tipo carpeta (tarjetas SD, cámaras, pendrives).

Rutas que usa Cloudy para Android — las mismas por MTP o por ADB, así una foto nunca se copia dos veces:
    android/<serie>/interno/DCIM/Camera/20240115_143012.jpg
    android/<serie>/sd-1A2B-3C4D/DCIM/Camera/20240115_143012.jpg
El prefijo con el número de serie evita que dos celulares distintos se pisen en el catálogo.
Todo es de SOLO LECTURA: ningún backend de Android tiene operaciones para borrar, mover ni escribir.
"""
from __future__ import annotations

import re
from typing import Optional

from ..organizer.layout import _safe

INTERNAL = "interno"

# Carpetas con fotos y videos dentro de cada almacenamiento.
CAMERA_ROOTS = ["DCIM", "Pictures", "Movies"]
MESSAGING_ROOTS = [
    # WhatsApp (Android 11+), WhatsApp Business y la ubicación vieja
    "Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
    "Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video",
    "Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Animated Gifs",
    "Android/media/com.whatsapp.w4b/WhatsApp Business/Media/WhatsApp Business Images",
    "Android/media/com.whatsapp.w4b/WhatsApp Business/Media/WhatsApp Business Video",
    "WhatsApp/Media/WhatsApp Images",
    "WhatsApp/Media/WhatsApp Video",
    # Telegram
    "Telegram/Telegram Images",
    "Telegram/Telegram Video",
    "Android/media/org.telegram.messenger/Telegram/Telegram Images",
    "Android/media/org.telegram.messenger/Telegram/Telegram Video",
]
DOWNLOAD_ROOTS = ["Download"]

# Carpetas que nunca se recorren (miniaturas, papelera, cachés, datos privados de apps).
SKIP_DIRS = {".thumbnails", "thumbnails", ".trash", ".trashed", ".recyclebin", "cache", ".cache", "lost.dir",
             "sent", ".statuses", ".private", "system volume information", "$recycle.bin", ".spotlight-v100",
             ".fseventsd", ".temporaryitems"}
SKIP_FILE_PREFIXES = (".", "~$")      # ocultos, «.trashed-…» y «.pending-…» de Android, temporales
MAX_DEPTH = 8


def skip_dir(name: str) -> bool:
    low = name.lower()
    return low.startswith(".") or low in SKIP_DIRS


def skip_file(name: str) -> bool:
    return name.startswith(SKIP_FILE_PREFIXES)


def media_roots(messaging: bool = True, downloads: bool = False) -> list[str]:
    roots = list(CAMERA_ROOTS)
    if messaging:
        roots += MESSAGING_ROOTS
    if downloads:
        roots += DOWNLOAD_ROOTS
    return roots


def album_for(rel: str) -> list[str]:
    """Álbum «lógico» según la carpeta (rel = ruta dentro del almacenamiento)."""
    low = rel.lower()
    if "/whatsapp" in "/" + low:
        return ["WhatsApp"]
    if "/telegram" in "/" + low:
        return ["Telegram"]
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] in ("Pictures", "DCIM") and parts[1].lower() not in ("camera", "100andro"):
        return [parts[1]]
    return []


def is_screenshot_path(rel: str) -> bool:
    low = rel.lower()
    return "/screenshots/" in "/" + low or "/screen recordings/" in "/" + low or \
        low.rsplit("/", 1)[-1].startswith(("screenshot_", "screenshot-", "screen_recording", "captura"))


_SERIAL_OK = re.compile(r"[^A-Za-z0-9._-]")


def safe_serial(serial: str) -> str:
    """El número de serie viene del aparato: se limpia para usarlo como parte de una ruta."""
    s = _SERIAL_OK.sub("_", serial or "")[:64].strip("._")
    return s or "sin-serie"


def storage_slug(description: str, index: int, is_removable: Optional[bool] = None,
                 volume_id: Optional[str] = None) -> str:
    """Nombre estable para cada almacenamiento: «interno» o «sd-<id>»."""
    low = (description or "").lower()
    removable = is_removable if is_removable is not None else any(
        k in low for k in ("sd", "tarjeta", "card", "extern", "removable", "usb"))
    if not removable and (index == 0 or any(k in low for k in ("intern", "phone", "teléfono", "telefono",
                                                                "shared", "compartido", "dispositivo"))):
        return INTERNAL
    vid = re.sub(r"[^A-Za-z0-9]", "", volume_id or "")[:16].upper()
    return f"sd-{vid}" if vid else f"sd-{index}"


def join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def clean_segment(name: str) -> str:
    """Un nombre de carpeta/archivo del aparato, seguro para mostrar y para usar en rutas locales."""
    return _safe(name)
