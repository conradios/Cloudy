"""Celular Android SIMULADO (tipo Samsung Galaxy) para probar la app sin conectar nada.

Arma en esta compu una carpeta con la estructura típica de un Samsung: cámara, capturas, WhatsApp,
Telegram, otra app, papelera y miniaturas (que NO se deben copiar), y una tarjeta SD.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..config import config_dir
from ..models import DeviceInfo
from .base import CHUNK_SIZE, DisconnectedError
from .demo import _bmff, _jpeg
from .folder import FolderBackend

DEMO_SERIAL = "DEMO-GALAXY-S24"

# (ruta, tipo 0=foto 1=video, fecha del archivo)
DEMO_FILES = [
    ("interno/DCIM/Camera/20230314_102100.jpg", 0, "2023-03-14 10:21"),
    ("interno/DCIM/Camera/20230802_190500.jpg", 0, "2023-08-02 19:05"),
    ("interno/DCIM/Camera/20240525_173000.mp4", 1, "2024-05-25 17:30"),
    ("interno/DCIM/Camera/20241224_210000.jpg", 0, "2024-12-24 21:00"),
    ("interno/DCIM/Screenshots/Screenshot_20240709_081100_Chrome.jpg", 0, "2024-07-09 08:11"),
    ("interno/DCIM/Camera/.trashed-1767225600-20220101_000000.jpg", 0, "2022-01-01 00:00"),   # papelera
    ("interno/DCIM/.thumbnails/1234567.jpg", 0, "2024-12-24 21:00"),                          # miniatura
    ("interno/Pictures/Instagram/IMG_20250115_140200_123.jpg", 0, "2025-01-15 14:02"),
    ("interno/Pictures/foto con 'comillas' y $(echo hola).jpg", 0, "2025-02-01 12:00"),
    ("interno/Movies/Grabaciones/VID_20250301_091500.mp4", 1, "2025-03-01 09:15"),
    ("interno/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-20250303-WA0001.jpg", 0,
     "2025-03-03 21:40"),
    ("interno/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/Sent/IMG-20250303-WA0002.jpg", 0,
     "2025-03-03 21:41"),                                                                          # enviada
    ("interno/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video/VID-20250621-WA0003.mp4", 1,
     "2025-06-21 18:44"),
    ("interno/Telegram/Telegram Images/554433_121.jpg", 0, "2025-08-30 11:11"),
    ("interno/Download/meme.png", 0, "2025-10-05 16:20"),                                         # descargas
    ("interno/Documents/contrato.pdf", 9, "2025-10-05 16:20"),                                    # no es foto
    ("sd-1A2B3C4D/DCIM/Camera/20260909_131300.jpg", 0, "2026-09-09 13:13"),
    ("sd-1A2B3C4D/DCIM/Camera/20261101_100000.mp4", 1, "2026-11-01 10:00"),
]


def demo_android_root() -> Path:
    return config_dir() / "demo_android"


def build_demo_android(root: Optional[Path] = None, reset: bool = False) -> Path:
    root = Path(root or demo_android_root())
    if reset and root.exists():
        shutil.rmtree(root)
    marker = root / ".cloudy-demo-v1"
    if marker.exists():
        return root
    for i, (rel, kind, when) in enumerate(DEMO_FILES):
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        if kind == 0:
            data = _jpeg(p.name, 500 + i, "PNG" if p.suffix.lower() == ".png" else "JPEG")
        elif kind == 1:
            data = _bmff(b"mp42", 2_500_000, 500 + i, video=True)
        else:
            data = b"%PDF-1.4\n% demo\n"
        p.write_bytes(data)
        ts = datetime.strptime(when, "%Y-%m-%d %H:%M").timestamp()
        os.utime(p, (ts, ts))
    marker.write_text("ok", encoding="utf-8")
    return root


class DemoAndroidBackend(FolderBackend):
    kind = "android"

    def __init__(self, root: Optional[Path] = None, slow: bool = True):
        super().__init__(root or demo_android_root(), label="Galaxy S24 (DEMO)")
        self.ns = f"android/{DEMO_SERIAL}"
        self.slow = slow
        self.recover_timeout = 60
        self.unplug_after_bytes: Optional[int] = None
        self.replug_after_seconds = 4.0
        self._unplugged_at: Optional[float] = None
        self._served = 0

    @property
    def is_demo(self) -> bool:
        return True

    def connect(self, on_status=None) -> DeviceInfo:
        if on_status:
            on_status("Conectando con el celular Android simulado…")
        build_demo_android(self.root)
        self._connected = True
        return DeviceInfo(name="Galaxy S24 (DEMO)", model="Samsung SM-S921B (simulado)", ios_version="15",
                          udid=DEMO_SERIAL, connection="Simulado", platform="Android", kind="android")

    def listdir(self, path: str) -> list[str]:
        if path.strip("/") == self.ns:
            return sorted(n for n in os.listdir(self._p(path)) if not n.startswith("."))
        return super().listdir(path)

    def simulate_unplug(self, after_bytes: int = 3_000_000, replug_after_seconds: float = 4.0):
        self._served = 0
        self.unplug_after_bytes = after_bytes
        self.replug_after_seconds = replug_after_seconds

    def is_present(self) -> bool:
        if self._unplugged_at is None:
            return True
        return time.monotonic() - self._unplugged_at >= self.replug_after_seconds

    def reconnect(self) -> None:
        self._unplugged_at = None
        self._connected = True

    def waiting_message(self) -> str:
        return "[DEMO] Se «desconectó» el Android simulado. Se reconecta solo en unos segundos…"

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        for data in super().read_chunks(path, min(chunk_size, 1024 * 1024), offset):
            if not self._connected:
                raise DisconnectedError("Cable desconectado")
            if self.slow:
                time.sleep(0.02)
            self._served += len(data)
            if self.unplug_after_bytes is not None and self._served > self.unplug_after_bytes:
                self.unplug_after_bytes = None
                self._connected = False
                self._unplugged_at = time.monotonic()
                raise DisconnectedError("Cable desconectado")
            yield data
