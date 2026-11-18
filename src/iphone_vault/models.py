"""Modelos de datos compartidos entre el escáner, el catálogo, la transferencia y la GUI."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Disponibilidad del original en el iPhone
AVAIL_LOCAL = "local"            # el original completo está en el iPhone
AVAIL_CLOUD_ONLY = "cloud_only"  # el asset existe en Fotos pero el archivo original no está en el iPhone
AVAIL_OPTIMIZED = "optimized"    # hay un archivo, pero más chico que el original (versión optimizada)
AVAIL_UNKNOWN = "unknown"        # no se pudo leer la base de Fotos: no sabemos

# Roles de archivo dentro de un elemento
ROLE_ORIGINAL = "original"
ROLE_LIVE_VIDEO = "live_video"
ROLE_AAE = "aae"
ROLE_EDITED = "edited"

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tif", ".tiff", ".webp", ".bmp", ".avif",
             ".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2", ".srw", ".pef"}   # RAW de cámaras
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".hevc", ".mkv", ".webm", ".mts", ".m2ts"}


@dataclass
class RemoteStat:
    size: int
    is_dir: bool
    mtime: Optional[datetime] = None
    birthtime: Optional[datetime] = None


@dataclass
class RemoteFile:
    path: str          # ruta en el origen, ej. "DCIM/100APPLE/IMG_0001.HEIC" o "icloud/<id>/original"
    size: int
    role: str = ROLE_ORIGINAL
    filename: Optional[str] = None   # nombre real si la ruta no lo contiene (iCloud)

    @property
    def name(self) -> str:
        return self.filename or self.path.rsplit("/", 1)[-1]


@dataclass
class MediaItem:
    key: str                         # UUID de Fotos si existe; si no, ruta del archivo principal
    primary_path: str                # ruta esperada del original
    kind: str                        # "photo" | "video"
    files: list[RemoteFile] = field(default_factory=list)
    availability: str = AVAIL_UNKNOWN
    capture_date: Optional[datetime] = None
    uuid: Optional[str] = None
    expected_size: Optional[int] = None   # tamaño original según la base de Fotos
    is_live: bool = False
    is_screenshot: bool = False
    is_hidden: bool = False
    is_favorite: bool = False
    has_edits: bool = False
    is_trashed: bool = False              # está en "Eliminado recientemente"
    albums: list[str] = field(default_factory=list)
    original_filename: Optional[str] = None
    in_photos_db: bool = False
    source: str = "usb"                    # "usb" (cable) | "icloud"
    display_name: Optional[str] = None

    @property
    def fingerprint(self) -> Optional[str]:
        """Identifica la MISMA foto venga del iPhone o de iCloud: nombre original + segundo de captura +
        tamaño del original completo. (Con el tamaño, dos fotos distintas con el mismo nombre y la misma
        hora nunca se confunden: preferimos una copia de más antes que perder una foto.)"""
        name = (self.original_filename or self.name or "").upper()
        if not name or self.capture_date is None:
            return None
        size = self.expected_size if self.expected_size else (self.files[0].size if self.files else None)
        if size is None:
            return None
        return f"{name}|{int(self.capture_date.timestamp())}|{size}"

    @property
    def name(self) -> str:
        return self.display_name or self.primary_path.rsplit("/", 1)[-1]

    @property
    def is_available(self) -> bool:
        return self.availability in (AVAIL_LOCAL, AVAIL_UNKNOWN)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def best_size(self) -> int:
        return self.total_size if self.files else (self.expected_size or 0)

    def files_by_role(self, role: str) -> list[RemoteFile]:
        return [f for f in self.files if f.role == role]


@dataclass
class DeviceInfo:
    name: str
    model: str
    ios_version: str          # versión del sistema (iOS, Android…); se conserva el nombre por compatibilidad
    udid: str
    connection: str = "USB"
    platform: str = "iOS"     # "iOS" | "Android" | "" (carpeta, tarjeta SD, cámara)
    kind: str = "iphone"      # "iphone" | "android" | "folder"

    @property
    def label(self) -> str:
        sysv = f"{self.platform} {self.ios_version}".strip() if self.ios_version else self.platform
        return " · ".join(x for x in (self.name, self.model if self.model != self.name else "", sysv) if x)


@dataclass
class ScanResult:
    items: list[MediaItem]
    photos_db_available: bool
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def local_items(self) -> list[MediaItem]:
        return [i for i in self.items if i.is_available]

    @property
    def unavailable_items(self) -> list[MediaItem]:
        return [i for i in self.items if not i.is_available]


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
