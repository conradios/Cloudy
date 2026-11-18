"""Verificación de ESTRUCTURA de los archivos copiados (además del tamaño exacto y el SHA-256).

Detecta archivos truncados o corruptos sin necesidad de decodificar la imagen/video:
  - HEIC/HEIF/MOV/MP4/M4V/3GP (ISO-BMFF): las «cajas» de primer nivel tienen que cubrir
    exactamente el archivo, sin pasarse ni quedar cortas; HEIC necesita `ftyp` + `meta`
    y los videos `moov` + `mdat`.
  - JPEG: empieza con FFD8 y termina con el marcador FFD9.
  - PNG: firma correcta y bloque IEND al final.
  - GIF, TIFF/DNG: cabecera y cierre.
Otros formatos (.AAE, .json…) no se validan por estructura.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

BMFF_EXT = {".heic", ".heif", ".mov", ".mp4", ".m4v", ".3gp", ".hevc", ".avif"}
VIDEO_BMFF = {".mov", ".mp4", ".m4v", ".3gp", ".hevc"}

OK, SKIPPED, FAILED = "ok", "sin validar", "falló"


@dataclass
class ValidationResult:
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAILED


def _bmff(path: Path, ext: str) -> ValidationResult:
    size = path.stat().st_size
    types = []
    with open(path, "rb") as fh:
        pos = 0
        while pos < size:
            fh.seek(pos)
            head = fh.read(8)
            if len(head) < 8:
                return ValidationResult(FAILED, f"caja incompleta en el byte {pos}")
            box_size, box_type = struct.unpack(">I4s", head)
            if box_size == 1:
                ext_size = fh.read(8)
                if len(ext_size) < 8:
                    return ValidationResult(FAILED, "tamaño extendido incompleto")
                box_size = struct.unpack(">Q", ext_size)[0]
            elif box_size == 0:
                box_size = size - pos
            if box_size < 8:
                return ValidationResult(FAILED, f"caja inválida en el byte {pos}")
            if pos + box_size > size:
                return ValidationResult(FAILED, f"archivo truncado: la caja '{box_type.decode('latin1')}' "
                                                f"necesita {pos + box_size - size} bytes más")
            types.append(box_type)
            pos += box_size
            if len(types) > 100_000:
                return ValidationResult(FAILED, "demasiadas cajas")
    if ext in VIDEO_BMFF:
        if b"moov" not in types:
            return ValidationResult(FAILED, "video sin índice (moov)")
        if b"mdat" not in types:
            return ValidationResult(FAILED, "video sin datos (mdat)")
    else:
        if not types or types[0] != b"ftyp":
            return ValidationResult(FAILED, "falta la cabecera ftyp")
        if b"meta" not in types:
            return ValidationResult(FAILED, "imagen sin metadatos (meta)")
    return ValidationResult(OK, f"{len(types)} cajas")


def _tail(path: Path, n: int) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - n))
        return fh.read()


def _head(path: Path, n: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def validate_file(path: Path) -> ValidationResult:
    ext = path.suffix.lower()
    if path.name.endswith(".part"):
        ext = Path(path.name[:-5]).suffix.lower()
    if path.stat().st_size == 0:
        return ValidationResult(FAILED, "archivo vacío")
    try:
        if ext in BMFF_EXT:
            return _bmff(path, ext)
        if ext in (".jpg", ".jpeg"):
            if _head(path, 3) != b"\xff\xd8\xff":
                return ValidationResult(FAILED, "no empieza como JPEG")
            if b"\xff\xd9" not in _tail(path, 64 * 1024):
                return ValidationResult(FAILED, "JPEG truncado (falta el cierre)")
            return ValidationResult(OK)
        if ext == ".png":
            if _head(path, 8) != b"\x89PNG\r\n\x1a\n":
                return ValidationResult(FAILED, "firma PNG inválida")
            if b"IEND" not in _tail(path, 32):
                return ValidationResult(FAILED, "PNG truncado (falta IEND)")
            return ValidationResult(OK)
        if ext == ".gif":
            if _head(path, 4) != b"GIF8" or not _tail(path, 1) == b";":
                return ValidationResult(FAILED, "GIF inválido o truncado")
            return ValidationResult(OK)
        if ext in (".dng", ".tif", ".tiff"):
            if _head(path, 4) not in (b"II*\x00", b"MM\x00*"):
                return ValidationResult(FAILED, "cabecera TIFF/DNG inválida")
            return ValidationResult(OK)
    except OSError as e:
        return ValidationResult(FAILED, f"no se pudo leer: {e}")
    return ValidationResult(SKIPPED)
