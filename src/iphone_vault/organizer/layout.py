"""Decide en qué carpeta y con qué nombre queda cada archivo dentro del destino."""
from __future__ import annotations

from pathlib import PurePosixPath

from ..config import LAYOUT_FLAT, LAYOUT_MIRROR, LAYOUT_MONTH, LAYOUT_YEAR
from ..models import ROLE_EDITED, MediaItem, RemoteFile

INVALID_CHARS = set('<>:"\\|?*/')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 100   # nombres cortos: Windows sin «rutas largas» falla por encima de 260 caracteres


def _safe(name: str) -> str:
    """Nombre de archivo seguro en Windows/macOS/Linux. Los nombres vienen del iPhone o de iCloud,
    así que se tratan como no confiables: sin separadores de carpeta (evita salir del destino con
    «../»), sin caracteres prohibidos, sin nombres reservados de Windows y con largo acotado."""
    name = "".join("_" if (c in INVALID_CHARS or ord(c) < 32) else c for c in str(name))
    name = name.strip(" .")
    if not name or set(name) <= {"_", "."}:
        name = "archivo"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext[:16]
    if stem.split(".")[0].upper() in RESERVED or (not stem and ext.upper() in RESERVED):
        stem = "_" + stem
    stem = stem[: MAX_NAME - len(ext) - 1] if ext else stem[:MAX_NAME]
    return f"{stem}.{ext}" if ext else stem


def folder_for_date(d, layout: str) -> str:
    if d is None:
        return "sin_fecha"
    if layout == LAYOUT_YEAR:
        return f"{d:%Y}"
    return f"{d:%Y-%m}" if layout == LAYOUT_MONTH else f"{d:%Y}/{d:%Y-%m}"


def item_folder(item: MediaItem, layout: str) -> str:
    if layout == LAYOUT_MIRROR:
        if item.source == "icloud":
            d = item.capture_date
            return "iCloud" if d is None else f"iCloud/{d:%Y}"
        folder = item.primary_path.rsplit("/", 1)[0] if "/" in item.primary_path else ""
        # cada carpeta viene del aparato: se limpia (sin «..», sin caracteres prohibidos ni reservados)
        return "/".join(_safe(seg) for seg in folder.split("/") if seg and seg not in (".", ".."))
    if layout == LAYOUT_FLAT:
        return ""
    return folder_for_date(item.capture_date, layout)


def local_relpath(item: MediaItem, f: RemoteFile, layout: str) -> str:
    """Ruta relativa (con '/') dentro de la carpeta destino."""
    folder = item_folder(item, layout)
    primary_stem = PurePosixPath(item.name).stem
    name = f.name
    if f.role == ROLE_EDITED and name.lower().startswith("fullsizerender"):
        # FullSizeRender.jpg → IMG_1234_editada.jpg (al lado del original)
        name = f"{primary_stem}_editada{PurePosixPath(name).suffix.lower()}"
    elif f.role == ROLE_EDITED and name.upper().startswith("IMG_E"):
        name = f"{primary_stem}_editada{PurePosixPath(name).suffix}"
    name = _safe(name)
    return f"{folder}/{name}" if folder else name


def with_suffix_number(relpath: str, n: int) -> str:
    p = PurePosixPath(relpath)
    return str(p.with_name(f"{p.stem}_{n}{p.suffix}"))
