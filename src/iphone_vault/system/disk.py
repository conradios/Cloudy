"""Revisa el disco de destino: espacio libre y formato (FAT32 no acepta archivos de más de 4 GB)."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FAT_LIMIT = 4 * 1024 ** 3 - 1
FAT_TYPES = {"fat", "fat32", "vfat", "msdos", "fat16", "fat12"}
LOW_SPACE_RATIO = 0.10
LOW_SPACE_BYTES = 20 * 1024 ** 3


@dataclass
class DiskInfo:
    path: str
    fstype: str
    total: int
    free: int

    @property
    def is_fat(self) -> bool:
        return self.fstype.lower() in FAT_TYPES

    @property
    def low_space(self) -> bool:
        return self.total > 0 and (self.free / self.total < LOW_SPACE_RATIO or self.free < LOW_SPACE_BYTES)

    @property
    def used_ratio(self) -> float:
        return 1 - self.free / self.total if self.total else 0.0


def _fstype(path: Path) -> str:
    try:
        import psutil
        target = str(path.resolve()).lower()
        best, best_len = "", -1
        for p in psutil.disk_partitions(all=True):
            mp = p.mountpoint.lower()
            if target.startswith(mp) and len(mp) > best_len:
                best, best_len = p.fstype, len(mp)
        return best or "?"
    except Exception:  # noqa: BLE001
        return "?"


def disk_info(path: str | Path) -> Optional[DiskInfo]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return None
    return DiskInfo(str(p), _fstype(p), u.total, u.free)


def too_big_for_fat(sizes: list[int]) -> int:
    """Cantidad de archivos que no entran en un disco FAT32."""
    return sum(1 for s in sizes if s > FAT_LIMIT)
