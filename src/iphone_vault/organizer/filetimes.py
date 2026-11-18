"""Fecha de captura como fechas del archivo, para verla en Propiedades sin archivos extra.

- Modificado: la fecha de captura (Windows, macOS y Linux).
- Creado: la fecha de captura (solo Windows; se usa la API SetFileTime).
Los metadatos internos de cada foto/video (EXIF: fecha, cámara, ubicación) ya están dentro del
archivo, porque la copia es idéntica byte por byte al original: no se agregan .json ni se modifica
el contenido.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

_EPOCH_AS_FILETIME = 116444736000000000   # 1970-01-01 en unidades de 100 ns desde 1601


def set_file_times(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES
        if handle in (None, wintypes.HANDLE(-1).value):
            return
        try:
            ft = int(ts * 10_000_000) + _EPOCH_AS_FILETIME
            created = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
            k32.SetFileTime(handle, ctypes.byref(created), None, None)
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
