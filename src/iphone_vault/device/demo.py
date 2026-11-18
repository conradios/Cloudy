"""iPhone SIMULADO para probar la app sin conectar nada.

Crea en esta compu una carpeta con la misma estructura que expone el iPhone por cable
(DCIM/100APPLE/…, PhotoData/Photos.sqlite, PhotoData/Mutations/…) con 17 elementos de prueba:
Live Photos, videos, una captura de pantalla, una foto editada, una oculta, una favorita
en un álbum, una en «Eliminado recientemente», y 3 cuyos originales están «solo en iCloud»
(una optimizada y dos sin archivo). Así se puede probar todo el flujo, incluida la lista de pendientes.
"""
from __future__ import annotations

import io
import os
import random
import shutil
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..config import config_dir
from ..models import DeviceInfo, RemoteStat
from .base import CHUNK_SIZE, DeviceBackend, DisconnectedError

CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# (carpeta, archivo, kind 0=foto 1=video, subtype, fecha, flags)
DEMO_ASSETS = [
    ("100APPLE", "IMG_0001.HEIC", 0, 2, "2023-03-14 10:21", {"live": True}),
    ("100APPLE", "IMG_0002.HEIC", 0, 2, "2023-08-02 19:05", {"live": True, "fav": True}),
    ("100APPLE", "IMG_0003.JPG", 0, 0, "2024-01-01 00:03", {}),
    ("100APPLE", "IMG_0004.HEIC", 0, 0, "2024-02-10 12:40", {"edited": True}),
    ("100APPLE", "IMG_0005.MOV", 1, 0, "2024-05-25 17:30", {}),
    ("100APPLE", "IMG_0006.PNG", 0, 10, "2024-07-09 08:11", {}),
    ("100APPLE", "IMG_0007.HEIC", 0, 0, "2024-09-18 22:47", {"hidden": True}),
    ("100APPLE", "IMG_0008.HEIC", 0, 0, "2024-12-24 21:00", {"fav": True, "album": "Vacaciones"}),
    ("100APPLE", "IMG_0009.MOV", 1, 0, "2025-01-15 14:02", {"album": "Vacaciones"}),
    ("100APPLE", "IMG_0010.HEIC", 0, 0, "2025-03-03 09:15", {}),
    ("100APPLE", "IMG_0011.JPG", 0, 0, "2025-06-21 18:44", {}),
    ("100APPLE", "IMG_0012.HEIC", 0, 0, "2025-08-30 11:11", {"optimized": True}),
    ("100APPLE", "IMG_0013.HEIC", 0, 0, "2025-10-05 16:20", {"cloud": True}),
    ("100APPLE", "IMG_0014.MOV", 1, 0, "2026-01-12 20:00", {"cloud": True}),
    ("100APPLE", "IMG_0015.HEIC", 0, 0, "2026-04-01 07:30", {"trashed": True}),
    ("101APPLE", "IMG_0016.HEIC", 0, 0, "2026-09-09 13:13", {}),
    ("101APPLE", "IMG_0017.JPG", 0, 0, "2026-11-01 10:00", {"album": "Vacaciones"}),
]


def demo_root() -> Path:
    return config_dir() / "demo_iphone"


def _jpeg(label: str, seed: int, fmt: str = "JPEG") -> bytes:
    try:
        from PIL import Image, ImageDraw
        rnd = random.Random(seed)
        img = Image.new("RGB", (1600, 1200), (rnd.randint(30, 220), rnd.randint(30, 220), rnd.randint(30, 220)))
        d = ImageDraw.Draw(img)
        d.rectangle([60, 60, 1540, 1140], outline=(255, 255, 255), width=12)
        d.text((120, 120), f"Cloudy DEMO\n{label}", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, fmt, **({"quality": 92} if fmt == "JPEG" else {}))
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return os.urandom(400_000)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _bmff(brand: bytes, size: int, seed: int, video: bool) -> bytes:
    """Contenedor ISO-BMFF válido (como HEIC / MOV) con contenido de relleno."""
    ftyp = _box(b"ftyp", brand + b"\0\0\0\0" + (b"qt  " if video else b"mif1heic"))
    if video:
        header = _box(b"moov", _box(b"mvhd", bytes(100)))
    else:
        header = _box(b"meta", b"\0\0\0\0" + _box(b"hdlr", bytes(8) + b"pict" + bytes(13)))
    body = random.Random(seed).randbytes(max(0, size - len(ftyp) - len(header) - 8))
    return ftyp + header + _box(b"mdat", body)


def _blob(size: int, seed: int) -> bytes:
    return _bmff(b"qt  ", size, seed, video=True)


def _heic(label: str, seed: int, small: bool) -> bytes:
    """HEIC real (si está pillow-heif): así el modo demo también prueba las copias JPG compatibles."""
    try:
        import pillow_heif
        from PIL import Image, ImageDraw
        pillow_heif.register_heif_opener()
        rnd = random.Random(seed)
        size = (400, 300) if small else (1600, 1200)
        img = Image.new("RGB", size, (rnd.randint(30, 220), rnd.randint(30, 220), rnd.randint(30, 220)))
        ImageDraw.Draw(img).text((40, 40), f"Cloudy DEMO (HEIC)\n{label}", fill=(255, 255, 255))
        exif = Image.Exif()
        exif[0x010F], exif[0x0110] = "Apple", "iPhone (demo)"
        buf = io.BytesIO()
        img.save(buf, "HEIF", quality=40 if small else 85, exif=exif.tobytes())
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return _bmff(b"heic", 60_000 if small else 1_800_000, seed, video=False)


def _full_content(name: str, kind: int, seed: int, small: bool = False) -> bytes:
    ext = name.rsplit(".", 1)[-1].upper()
    if ext in ("JPG", "JPEG", "PNG"):
        return _jpeg(name, seed, "PNG" if ext == "PNG" else "JPEG")
    if kind == 1:
        return _bmff(b"qt  ", 300_000 if small else 6_000_000, seed, video=True)
    return _heic(name, seed, small)


def build_demo_library(root: Optional[Path] = None, reset: bool = False) -> Path:
    root = Path(root or demo_root())
    if reset and root.exists():
        shutil.rmtree(root)
    if (root / "PhotoData" / "Photos.sqlite").exists():
        return root
    (root / "PhotoData").mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(root / "PhotoData" / "Photos.sqlite"))
    con.executescript("""
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT, ZKIND INTEGER,
            ZKINDSUBTYPE INTEGER, ZDATECREATED REAL, ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER, ZFAVORITE INTEGER,
            ZHASADJUSTMENTS INTEGER, ZSAVEDASSETTYPE INTEGER);
        CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER,
            ZORIGINALFILESIZE INTEGER, ZORIGINALFILENAME TEXT);
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER);
        CREATE TABLE Z_28ASSETS (Z_28ALBUMS INTEGER, Z_3ASSETS INTEGER, Z_FOK_3ASSETS INTEGER);
        INSERT INTO ZGENERICALBUM VALUES (1, 'Vacaciones', 2);
    """)
    for pk, (folder, name, kind, subtype, when, flags) in enumerate(DEMO_ASSETS, start=1):
        dcim = root / "DCIM" / folder
        dcim.mkdir(parents=True, exist_ok=True)
        content = _full_content(name, kind, pk)
        if not flags.get("cloud"):
            if flags.get("optimized"):
                (dcim / name).write_bytes(_full_content(name, kind, pk, small=True))   # versión reducida
            else:
                (dcim / name).write_bytes(content)
        stem = name.rsplit(".", 1)[0]
        if flags.get("live"):
            (dcim / f"{stem}.MOV").write_bytes(_blob(2_500_000, pk + 1000))
        if flags.get("edited"):
            adj = root / "PhotoData" / "Mutations" / "DCIM" / folder / stem / "Adjustments"
            adj.mkdir(parents=True, exist_ok=True)
            (adj / "FullSizeRender.jpg").write_bytes(_jpeg(f"{name} (editada)", pk + 2000))
            (dcim / f"{stem}.AAE").write_bytes(b"<?xml version='1.0'?><plist><dict/></plist>")
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M").astimezone()
        ts = dt.timestamp()
        if (dcim / name).exists():
            os.utime(dcim / name, (ts, ts))
        con.execute("INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            pk, f"DEMO-{pk:04d}-UUID", f"DCIM/{folder}", name, kind, subtype,
            (dt - CORE_DATA_EPOCH).total_seconds(), int(bool(flags.get("trashed"))), int(bool(flags.get("hidden"))),
            int(bool(flags.get("fav"))), int(bool(flags.get("edited"))), 3))
        con.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?,?,?,?)", (pk, pk, len(content), name))
        if flags.get("album"):
            con.execute("INSERT INTO Z_28ASSETS VALUES (1, ?, ?)", (pk, pk))
    con.commit()
    con.close()
    return root


def simulate_originals_downloaded(root: Optional[Path] = None) -> int:
    """Simula que en el iPhone se activó «Descargar y conservar originales» y terminó de bajar."""
    root = Path(root or demo_root())
    n = 0
    for pk, (folder, name, kind, _s, when, flags) in enumerate(DEMO_ASSETS, start=1):
        if flags.get("cloud") or flags.get("optimized"):
            target = root / "DCIM" / folder / name
            content = _full_content(name, kind, pk)
            if not target.exists() or target.stat().st_size < len(content):
                target.write_bytes(content)
                ts = datetime.strptime(when, "%Y-%m-%d %H:%M").astimezone().timestamp()
                os.utime(target, (ts, ts))
                n += 1
    return n


class DemoBackend(DeviceBackend):
    source = "usb"

    def __init__(self, root: Optional[Path] = None, slow: bool = True):
        self.root = Path(root or demo_root())
        self.slow = slow
        self._connected = False
        self.unplug_after_bytes: Optional[int] = None   # para probar cortes de cable
        self.replug_after_seconds: float = 4.0
        self._unplugged_at: Optional[float] = None
        self._served = 0
        self.recover_timeout = 60

    def simulate_unplug(self, after_bytes: int = 3_000_000, replug_after_seconds: float = 4.0):
        """Simula que se desenchufa el cable después de `after_bytes` y se vuelve a enchufar solo."""
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
        return ("[DEMO] Se «desconectó» el iPhone simulado. Se reconecta solo en unos segundos y sigo "
                "donde quedé.")

    @property
    def is_demo(self) -> bool:
        return True

    def connect(self, on_status=None) -> DeviceInfo:
        if on_status:
            on_status("Conectando con el iPhone simulado…")
        build_demo_library(self.root)
        self._connected = True
        return DeviceInfo(name="iPhone de prueba (DEMO)", model="iPhone simulado", ios_version="18.0",
                          udid="DEMO-0000", connection="Simulado")

    def close(self) -> None:
        self._connected = False

    def _p(self, path: str) -> Path:
        if not self._connected:
            raise DisconnectedError("El iPhone simulado no está conectado.")
        p = (self.root / path.lstrip("/")).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise PermissionError(path)
        return p

    def listdir(self, path: str) -> list[str]:
        return sorted(os.listdir(self._p(path)))

    def stat(self, path: str) -> Optional[RemoteStat]:
        p = self._p(path)
        if not p.exists():
            return None
        st = p.stat()
        return RemoteStat(size=st.st_size, is_dir=p.is_dir(), mtime=datetime.fromtimestamp(st.st_mtime))

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        with open(self._p(path), "rb") as fh:
            fh.seek(offset)
            while True:
                if not self._connected:
                    raise DisconnectedError("Cable desconectado")
                data = fh.read(min(chunk_size, 1024 * 1024))
                if not data:
                    break
                if self.slow:
                    time.sleep(0.02)   # simula la velocidad del cable
                self._served += len(data)
                if self.unplug_after_bytes is not None and self._served > self.unplug_after_bytes:
                    self.unplug_after_bytes = None
                    self._connected = False
                    self._unplugged_at = time.monotonic()
                    raise DisconnectedError("Cable desconectado")
                yield data
