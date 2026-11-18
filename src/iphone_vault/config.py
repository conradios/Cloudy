"""Opciones de transferencia y configuración persistente (guardada solo en esta compu)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

LAYOUT_YEAR = "por_anio"       # 2024/IMG_0001.HEIC  (una carpeta por año)
LAYOUT_MONTH = "por_mes"       # 2024-07/IMG_0001.HEIC  (una carpeta por mes, todas al mismo nivel)
LAYOUT_BY_DATE = "por_fecha"   # 2024/2024-07/IMG_0001.HEIC
LAYOUT_MIRROR = "espejo"       # DCIM/100APPLE/IMG_0001.HEIC
LAYOUT_FLAT = "plano"          # IMG_0001.HEIC

LAYOUT_LABELS = {
    LAYOUT_YEAR: "Una carpeta por año (2024)",
    LAYOUT_MONTH: "Una carpeta por mes (2024-07)",
    LAYOUT_BY_DATE: "Año y mes (2024 / 2024-07)",
    LAYOUT_MIRROR: "Igual que en el iPhone (DCIM/100APPLE)",
    LAYOUT_FLAT: "Todo en una sola carpeta",
}


@dataclass
class TransferOptions:
    include_photos: bool = True
    include_videos: bool = True
    include_live_videos: bool = True      # el .MOV de las Live Photos
    include_screenshots: bool = True
    include_hidden: bool = True           # álbum "Oculto" de Fotos
    include_edited: bool = True           # versión editada (FullSizeRender), además del original
    include_aae: bool = False             # archivos .AAE de ajustes de edición
    write_sidecar_json: bool = False      # .json aparte: desactivado (los metadatos van dentro de cada archivo)
    verify_reread: bool = True            # relee el archivo del disco y compara SHA-256
    verify_structure: bool = True         # valida la estructura interna (HEIC/MOV/JPEG/PNG…)
    preserve_dates: bool = True           # pone la fecha de captura como fecha del archivo
    compat_photos: bool = False           # además, copia JPG de cada HEIC (solo si no podés instalar los códecs)
    compat_videos: bool = False           # además, copia MP4 (H.264) de cada video HEVC (ídem)
    only_favorites: bool = False
    layout: str = LAYOUT_YEAR
    limit: int = 10                       # 0 = sin límite (para pruebas empezamos con 10)
    newest_first: bool = True
    date_from: Optional[str] = None       # "YYYY-MM-DD"
    date_to: Optional[str] = None
    mirror_destinations: list = field(default_factory=list)   # copias extra: NAS, disco externo, OneDrive…
    # Android / tarjetas / cámaras
    android_messaging: bool = True        # fotos y videos recibidos por WhatsApp y Telegram
    android_downloads: bool = False       # fotos y videos de la carpeta Descargas
    verify_on_device: bool = True         # (ADB) además compara el SHA-256 calculado en el propio celular


@dataclass
class CleanupRules:
    """«Liberar espacio»: qué se puede mandar a «Eliminado recientemente» de iCloud."""
    min_size_mb: int = 100
    only_videos: bool = True
    older_than_days: int = 90
    batch_size: int = 25


SETTINGS_VERSION = 2


@dataclass
class AppSettings:
    settings_version: int = SETTINGS_VERSION
    destination: str = ""
    demo_mode: bool = False
    auto_sync_usb: bool = False           # al conectar el iPhone, copiar lo nuevo solo
    icloud_enabled: bool = False
    icloud_apple_id: str = ""             # solo el correo; la contraseña NUNCA se guarda
    icloud_auto_hours: int = 0            # 0 = no revisar iCloud automáticamente
    icloud_allow_phone_data: bool = False  # permitir iCloud aunque la compu salga por el hotspot del iPhone
    onboarding_done: bool = False         # ya vio la guía de primer uso
    notify_windows: bool = True           # avisos del sistema (bandeja) cuando la app no está al frente
    verify_reminder_days: int = 30        # recordar verificar la copia cada N días (0 = nunca)
    options: TransferOptions = field(default_factory=TransferOptions)
    cleanup: CleanupRules = field(default_factory=CleanupRules)


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "iPhoneVault"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "iPhoneVault"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "iphone-vault"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_settings() -> AppSettings:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AppSettings()
    def pick(cls, data):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    s = pick(AppSettings, {k: v for k, v in raw.items() if k not in ("options", "cleanup")})
    s.options = pick(TransferOptions, raw.get("options"))
    s.cleanup = pick(CleanupRules, raw.get("cleanup"))
    if int(raw.get("settings_version", 1)) < 2:
        # v0.4: una carpeta por año, sin .json (los metadatos van dentro de cada archivo)
        s.options.layout = LAYOUT_YEAR
        s.options.write_sidecar_json = False
        s.settings_version = SETTINGS_VERSION
    if "onboarding_done" not in raw and s.destination:
        s.onboarding_done = True          # quien ya venía usando la app no necesita la guía
    return s


def save_settings(settings: AppSettings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
