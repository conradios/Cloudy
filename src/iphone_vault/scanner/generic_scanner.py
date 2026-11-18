"""Escaneo de celulares Android y de carpetas (tarjetas SD, cámaras, pendrives).

A diferencia del iPhone no hay una «base de Fotos» que leer: cada archivo de foto o video es un elemento.
La fecha sale del nombre del archivo (20240115_143012.jpg, IMG-20240115-WA0001.jpg…) y, si no la tiene,
de la fecha del archivo (que en cámaras y tarjetas SD es la de la toma).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Iterator, Optional

from ..device.android_common import (MAX_DEPTH, album_for, is_screenshot_path, join, media_roots, skip_dir,
                                     skip_file)
from ..device.base import DeviceBackend
from ..models import AVAIL_LOCAL, PHOTO_EXT, VIDEO_EXT, MediaItem, RemoteFile, ScanResult
from ..organizer.dates import date_from_filename

log = logging.getLogger(__name__)
ProgressCb = Callable[[str, int, int], None]
Entry = tuple[str, int, Optional[datetime]]


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def backend_kind(backend: DeviceBackend) -> str:
    return getattr(backend, "kind", "iphone")


def walk(backend: DeviceBackend, top: str, depth: int = 0) -> Iterator[Entry]:
    """Recorre `top` con listdir/stat (sirve para cualquier backend). Usa el atajo del backend si existe."""
    fast = getattr(backend, "walk_files", None)
    if fast is not None and depth == 0:
        yield from fast(top)
        return
    if depth > MAX_DEPTH:
        return
    try:
        names = backend.listdir(top)
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001
        if backend.is_connection_error(e):
            raise
        log.info("No se pudo listar %s: %s", top, type(e).__name__)
        return
    paths = [join(top, n) for n in names if n not in (".", "..")]
    stats = backend.stat_many(paths)
    for p in paths:
        st = stats.get(p)
        if st is None:
            continue
        name = p.rsplit("/", 1)[-1]
        if st.is_dir:
            if not skip_dir(name):
                yield from walk(backend, p, depth + 1)
        elif not skip_file(name):
            yield p, st.size, st.mtime


class GenericScanner:
    def __init__(self, backend: DeviceBackend, progress: Optional[ProgressCb] = None,
                 messaging: bool = True, downloads: bool = False):
        self.backend = backend
        self.progress = progress or (lambda msg, done, total: None)
        self.messaging = messaging
        self.downloads = downloads

    # ------------------------------------------------------------------ raíces
    def _tops(self, warnings: list[str]) -> list[tuple[str, str]]:
        """(ruta completa, ruta del almacenamiento) de cada carpeta a recorrer."""
        ns = getattr(self.backend, "ns", "")
        if backend_kind(self.backend) == "folder":
            return [(ns, ns)]
        storages = self.backend.listdir(ns)
        if not storages:
            warnings.append("El celular no dejó ver sus archivos. Desbloquealo y, en el aviso «Usar USB para», "
                            "elegí «Transferencia de archivos».")
        tops = []
        for st in storages:
            base = join(ns, st)
            for r in media_roots(self.messaging, self.downloads):
                tops.append((join(base, r), base))
        return tops

    # ------------------------------------------------------------------ escaneo
    def scan(self) -> ScanResult:
        warnings: list[str] = []
        self.progress("Buscando fotos y videos…", 0, 0)
        tops = self._tops(warnings)
        seen: set[str] = set()
        items: list[MediaItem] = []
        skipped_other = 0
        for i, (top, base) in enumerate(tops):
            if self.backend.stat(top) is None:
                continue
            short = top[len(base):].strip("/") or top.rsplit("/", 1)[-1]
            self.progress(f"Revisando {short}…", i, len(tops))
            for path, size, mtime in walk(self.backend, top):
                if path in seen:
                    continue
                seen.add(path)
                rel = path[len(base):].strip("/")
                if any(skip_dir(seg) for seg in rel.split("/")[:-1]):
                    continue
                name = path.rsplit("/", 1)[-1]
                ext = _ext(name)
                if skip_file(name) or size <= 0:
                    continue
                if ext in PHOTO_EXT:
                    kind = "photo"
                elif ext in VIDEO_EXT:
                    kind = "video"
                else:
                    skipped_other += 1
                    continue
                date = date_from_filename(name)
                local_m = mtime.astimezone() if mtime else None
                if date is None:
                    date = local_m
                elif local_m and (date.hour, date.minute, date.second) == (12, 0, 0) and \
                        local_m.date() == date.date():
                    date = local_m      # el nombre solo trae el día (WhatsApp): la hora sale del archivo
                items.append(MediaItem(
                    key=path, primary_path=path, kind=kind,
                    files=[RemoteFile(path=path, size=size)],
                    availability=AVAIL_LOCAL, capture_date=date, expected_size=size,
                    is_screenshot=is_screenshot_path(rel), albums=album_for(rel),
                    original_filename=name, in_photos_db=False, source="usb"))
                if len(items) % 200 == 0:
                    self.progress(f"Encontré {len(items)} fotos y videos…", i, len(tops))
        items.sort(key=lambda it: (it.capture_date is None, it.capture_date or datetime.min), reverse=False)
        self.progress(f"Listo: {len(items)} fotos y videos.", len(tops), len(tops))
        return ScanResult(items=items, photos_db_available=False, warnings=warnings,
                          stats={"files": len(items), "other_files_skipped": skipped_other,
                                 "photos": sum(1 for x in items if x.kind == "photo"),
                                 "videos": sum(1 for x in items if x.kind == "video")})


def scan_backend(backend: DeviceBackend, progress: Optional[ProgressCb] = None, options=None) -> ScanResult:
    """Elige el escáner según el tipo de fuente."""
    if backend_kind(backend) in ("android", "folder"):
        return GenericScanner(backend, progress,
                              messaging=getattr(options, "android_messaging", True),
                              downloads=getattr(options, "android_downloads", False)).scan()
    from .media_scanner import MediaScanner
    return MediaScanner(backend, progress).scan()
