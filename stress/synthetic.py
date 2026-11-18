"""iPhone sintético para pruebas de estrés: genera fototecas enormes SIN ocupar disco del lado del origen.

El contenido de cada archivo se genera al vuelo, de forma determinística (mismo archivo = mismos bytes),
con estructura válida (HEIC/MOV ISO-BMFF, JPEG). Permite inyectar latencia, límite de velocidad,
cortes de conexión, errores de lectura, datos truncados o de más.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iphone_vault.device.base import DeviceBackend, DisconnectedError  # noqa: E402
from iphone_vault.models import DeviceInfo, RemoteStat  # noqa: E402

CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_BUF = random.Random(1234).randbytes(8 * 1024 * 1024)


def _box(kind: bytes, payload_len: int, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + payload_len) + kind + payload


@dataclass
class VFile:
    path: str
    size: int
    seed: int
    kind: str      # heic | mov | jpg | png | aae

    def header(self) -> bytes:
        if self.kind == "heic":
            ftyp = _box(b"ftyp", 16, b"heic\0\0\0\0mif1heic")
            meta = _box(b"meta", 12, b"\0\0\0\0" + b"\0" * 8)
            return ftyp + meta + struct.pack(">I", self.size - len(ftyp) - len(meta)) + b"mdat"
        if self.kind == "mov":
            ftyp = _box(b"ftyp", 12, b"qt  \0\0\0\0qt  ")
            moov = _box(b"moov", 16, _box(b"mvhd", 8, b"\0" * 8))
            return ftyp + moov + struct.pack(">I", self.size - len(ftyp) - len(moov)) + b"mdat"
        if self.kind == "jpg":
            return b"\xff\xd8\xff\xe0"
        if self.kind == "png":
            return b"\x89PNG\r\n\x1a\n"
        return b""

    def trailer(self) -> bytes:
        if self.kind == "jpg":
            return b"\xff\xd9"
        if self.kind == "png":
            return b"\0\0\0\0IEND\xaeB`\x82"
        return b""

    def read(self, offset: int, n: int) -> bytes:
        """Bytes [offset, offset+n) del archivo virtual."""
        h, t = self.header(), self.trailer()
        end = min(self.size, offset + n)
        out = bytearray()
        pos = offset
        body_end = self.size - len(t)
        while pos < end:
            if pos < len(h):
                take = min(end, len(h)) - pos
                out += h[pos:pos + take]
            elif pos < body_end:
                take = min(end, body_end) - pos
                start = (pos + self.seed * 7919) % len(_BUF)
                chunk = _BUF[start:start + take]
                while len(chunk) < take:
                    chunk += _BUF[: take - len(chunk)]
                out += chunk
            else:
                k = pos - body_end
                take = min(end - pos, len(t) - k)
                out += t[k:k + take]
            pos += take
        return bytes(out)

    def sha256(self) -> str:
        h = hashlib.sha256()
        for off in range(0, self.size, 4 * 1024 * 1024):
            h.update(self.read(off, 4 * 1024 * 1024))
        return h.hexdigest()


@dataclass
class Faults:
    latency_s: float = 0.0              # por llamada (stat/listdir/open)
    throughput_mbps: float = 0.0        # 0 = sin límite
    disconnect_every_bytes: int = 0     # 0 = nunca
    disconnect_down_s: float = 0.2
    flaky_read_prob: float = 0.0        # error de lectura NO de conexión
    truncate_paths: set = field(default_factory=set)    # entrega menos bytes sin error
    overflow_paths: set = field(default_factory=set)    # entrega bytes de más
    never_come_back: bool = False


class SyntheticLibrary:
    """Genera la fototeca: archivos virtuales + Photos.sqlite real (en un temporal)."""

    def __init__(self, n_assets: int, seed: int = 7, size_scale: float = 1.0, cloud_only_ratio: float = 0.05,
                 big_video_bytes: int = 0, per_folder: int = 1000, fixed_size: int = 0):
        rnd = random.Random(seed)
        self.files: dict[str, VFile] = {}
        self.assets = []   # filas para la base
        start = datetime(2018, 1, 1, tzinfo=timezone.utc)
        for i in range(n_assets):
            folder = f"{100 + i // per_folder}APPLE"
            num = i % 10000
            r = rnd.random()
            when = start + timedelta(seconds=int(i * 9000 + rnd.random() * 3600))
            cloud = rnd.random() < cloud_only_ratio
            if r < 0.70:
                name, kind, k, sub = f"IMG_{num:04d}.HEIC", "heic", 0, 0
                size = int(rnd.uniform(1.5e6, 4e6) * size_scale)
            elif r < 0.85:
                name, kind, k, sub = f"IMG_{num:04d}.HEIC", "heic", 0, 2
                size = int(rnd.uniform(1.5e6, 3e6) * size_scale)
            elif r < 0.95:
                name, kind, k, sub = f"IMG_{num:04d}.MOV", "mov", 1, 0
                size = int(rnd.uniform(10e6, 120e6) * size_scale)
            else:
                name, kind, k, sub = f"IMG_{num:04d}.PNG", "png", 0, 10
                size = int(rnd.uniform(0.3e6, 2e6) * size_scale)
            if fixed_size:
                size = fixed_size
            size = max(size, 256)
            path = f"DCIM/{folder}/{name}"
            if not cloud:
                self.files[path] = VFile(path, size, i, kind)
                if sub == 2:
                    mp = path.rsplit(".", 1)[0] + ".MOV"
                    self.files[mp] = VFile(mp, max(256, int(2.5e6 * size_scale)) if not fixed_size else fixed_size,
                                           i + 10_000_000, "mov")
            self.assets.append((i + 1, f"UUID-{i:07d}", f"DCIM/{folder}", name, k, sub,
                                (when - CORE_DATA_EPOCH).total_seconds(), size))
        if big_video_bytes:
            path = "DCIM/999APPLE/IMG_9999.MOV"
            self.files[path] = VFile(path, big_video_bytes, 999_999, "mov")
            self.assets.append((n_assets + 1, "UUID-BIG", "DCIM/999APPLE", "IMG_9999.MOV", 1, 0,
                                (datetime(2026, 1, 1, tzinfo=timezone.utc) - CORE_DATA_EPOCH).total_seconds(),
                                big_video_bytes))
        self.dirs: dict[str, list[str]] = {}
        for p in self.files:
            d, n = p.rsplit("/", 1)
            self.dirs.setdefault(d, []).append(n)
        self.dirs["DCIM"] = sorted({d.split("/")[1] for d in self.dirs if d.startswith("DCIM/")})
        self._db = self._build_db()
        self.files["PhotoData/Photos.sqlite"] = None   # se sirve desde el archivo real

    def _build_db(self) -> bytes:
        tmp = Path(tempfile.mkdtemp()) / "Photos.sqlite"
        con = sqlite3.connect(tmp)
        con.executescript("""
            CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT, ZKIND INTEGER,
                ZKINDSUBTYPE INTEGER, ZDATECREATED REAL, ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER, ZFAVORITE INTEGER,
                ZHASADJUSTMENTS INTEGER, ZSAVEDASSETTYPE INTEGER);
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER,
                ZORIGINALFILESIZE INTEGER, ZORIGINALFILENAME TEXT);""")
        con.executemany("INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,0,0,0,0,3)", [a[:7] for a in self.assets])
        con.executemany("INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?,?,?,?)",
                        [(a[0], a[0], a[7], a[3]) for a in self.assets])
        con.commit()
        con.close()
        data = tmp.read_bytes()
        tmp.unlink()
        return data

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files.values() if f)


class SyntheticBackend(DeviceBackend):
    source = "usb"

    def __init__(self, lib: SyntheticLibrary, faults: Optional[Faults] = None):
        self.lib = lib
        self.f = faults or Faults()
        self.connected = False
        self.calls = {"listdir": 0, "stat": 0, "open": 0, "bytes": 0, "disconnects": 0, "flaky": 0}
        self._served_since_drop = 0
        self._down_until = 0.0
        self._lock = threading.Lock()
        self.recover_timeout = 5 if self.f.never_come_back else 60
        self._rnd = random.Random(99)

    def connect(self, on_status=None) -> DeviceInfo:
        self.connected = True
        return DeviceInfo("iPhone sintético", "Synthetic", "18.0", "SYN-1")

    def close(self):
        self.connected = False

    def _lat(self):
        if self.f.latency_s:
            time.sleep(self.f.latency_s)

    def _need(self):
        if not self.connected:
            raise DisconnectedError("no conectado")

    def listdir(self, path):
        self._need()
        self._lat()
        self.calls["listdir"] += 1
        if path == "DCIM":
            return list(self.lib.dirs["DCIM"])
        if path in self.lib.dirs:
            return list(self.lib.dirs[path])
        raise FileNotFoundError(path)

    def stat(self, path):
        self._need()
        self._lat()
        self.calls["stat"] += 1
        if path == "PhotoData/Photos.sqlite":
            return RemoteStat(len(self.lib._db), False)
        if path in self.lib.dirs and path != "DCIM":
            return RemoteStat(0, True)
        f = self.lib.files.get(path)
        if f is None:
            return None
        return RemoteStat(f.size, False, mtime=datetime(2024, 1, 1))

    def stat_many(self, paths):
        # el backend real hace 32 consultas en paralelo; acá contamos y aplicamos 1/32 de la latencia
        lat, self.f.latency_s = self.f.latency_s, self.f.latency_s / 32
        try:
            return {p: self.stat(p) for p in paths}
        finally:
            self.f.latency_s = lat

    def is_present(self):
        return not self.f.never_come_back and time.monotonic() >= self._down_until

    def reconnect(self):
        if not self.is_present():
            raise DisconnectedError("todavía desconectado")
        self.connected = True

    def read_chunks(self, path, chunk_size=4 * 1024 * 1024, offset=0) -> Iterator[bytes]:
        self._need()
        self._lat()
        self.calls["open"] += 1
        if path == "PhotoData/Photos.sqlite":
            yield self.lib._db[offset:]
            return
        f = self.lib.files.get(path)
        if f is None:
            raise FileNotFoundError(path)
        size = f.size
        if path in self.f.truncate_paths:
            size = max(1, size // 2)
        pos = offset
        while pos < size:
            self._need()
            n = min(chunk_size, size - pos, 1024 * 1024)
            if self.f.flaky_read_prob and self._rnd.random() < self.f.flaky_read_prob:
                self.calls["flaky"] += 1
                raise ValueError("error de lectura simulado")
            data = f.read(pos, n)
            if self.f.throughput_mbps:
                time.sleep(len(data) / (self.f.throughput_mbps * 1024 * 1024))
            with self._lock:
                self._served_since_drop += len(data)
                if self.f.disconnect_every_bytes and self._served_since_drop >= self.f.disconnect_every_bytes:
                    self._served_since_drop = 0
                    self.connected = False
                    self.calls["disconnects"] += 1
                    self._down_until = time.monotonic() + self.f.disconnect_down_s
                    raise DisconnectedError("cable desconectado (simulado)")
            self.calls["bytes"] += len(data)
            pos += len(data)
            yield data
        if path in self.f.overflow_paths:
            while True:            # un origen hostil que nunca termina
                yield b"\0" * (1024 * 1024)
