"""Copias compatibles con Windows: HEIC → JPG y videos HEVC → MP4 (H.264).

Los ORIGINALES no se tocan nunca: las copias compatibles van a una carpeta aparte dentro del destino,
«Compatibles (JPG-MP4)», con la misma organización por año/mes. Así podés abrir todo con el visor de
fotos y el reproductor de Windows sin instalar códecs, y seguís teniendo el original en máxima calidad.

- Fotos: se decodifican con pillow-heif (libheif incluida) y se guardan como JPG de alta calidad,
  conservando EXIF (fecha, ubicación, cámara) y el perfil de color.
- Videos: solo se convierten los que están en HEVC (H.265). Se usa el ffmpeg que viene con
  imageio-ffmpeg (no hace falta instalar nada). El audio se copia tal cual.
Todo se procesa en esta compu: no se usa internet.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

log = logging.getLogger(__name__)

COMPAT_DIR = "Compatibles (JPG-MP4)"
PHOTO_IN = {".heic", ".heif"}
VIDEO_IN = {".mov", ".mp4", ".m4v"}
JPEG_QUALITY = 92
VIDEO_CRF = 20

_heif_ready = False


def _no_window() -> dict:
    """En Windows, que ffmpeg no abra una ventana negra de consola."""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def compat_missing_libs(photos: bool = True, videos: bool = True) -> list[str]:
    """Librerías que faltan para crear las copias compatibles (lista vacía = todo bien)."""
    import importlib.util
    missing = []
    if photos and importlib.util.find_spec("pillow_heif") is None:
        missing.append("pillow-heif")
    if videos and importlib.util.find_spec("imageio_ffmpeg") is None:
        missing.append("imageio-ffmpeg")
    return missing


def compat_rel(rel: str) -> Optional[str]:
    """Ruta (relativa al destino) de la copia compatible, o None si ese archivo no necesita copia."""
    p = PurePosixPath(rel)
    if p.parts and p.parts[0] == COMPAT_DIR:
        return None
    ext = p.suffix.lower()
    if ext in PHOTO_IN:
        return str(PurePosixPath(COMPAT_DIR) / p.with_suffix(".jpg"))
    if ext in VIDEO_IN:
        return str(PurePosixPath(COMPAT_DIR) / p.with_suffix(".mp4"))
    return None


def is_hevc(src: Path) -> bool:
    exe = ffmpeg_exe()
    if not exe:
        return False
    r = subprocess.run([exe, "-hide_banner", "-i", str(src)], capture_output=True, text=True,  # nosec B603
                       errors="replace", **_no_window())
    return bool(re.search(r"Video:\s*(hevc|h265)", r.stderr, re.I))


def photo_to_jpg(src: Path, dst: Path) -> None:
    global _heif_ready
    import pillow_heif
    from PIL import Image
    if not _heif_ready:
        pillow_heif.register_heif_opener()
        _heif_ready = True
    with Image.open(src) as im:
        exif = im.info.get("exif")
        icc = im.info.get("icc_profile")
        rgb = im.convert("RGB")
    kwargs = {"quality": JPEG_QUALITY, "subsampling": 0, "optimize": True}
    if exif:
        kwargs["exif"] = exif
    if icc:
        kwargs["icc_profile"] = icc
    tmp = dst.with_name(dst.name + ".part")
    rgb.save(tmp, "JPEG", **kwargs)
    os.replace(tmp, dst)


def video_to_mp4(src: Path, dst: Path, cancel: Optional[threading.Event] = None) -> None:
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("no está ffmpeg (falta la librería imageio-ffmpeg)")
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF),
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "0", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,  # nosec B603 - ffmpeg propio,
                            **_no_window())                                     # rutas absolutas, sin shell
    while True:
        try:
            _, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise InterruptedError("cancelado")
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError((err or b"").decode(errors="replace").strip()[-300:] or "ffmpeg falló")
    os.replace(tmp, dst)


def make_compatible(root: Path, rel: str, videos: bool = True,
                    cancel: Optional[threading.Event] = None, photos: bool = True) -> Optional[str]:
    """Crea la copia compatible de <root>/<rel> si hace falta. Devuelve la ruta creada (relativa) o None."""
    out_rel = compat_rel(rel)
    if out_rel is None:
        return None
    src, dst = root / rel, root / out_rel
    if not src.exists() or (dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime):
        return None
    ext = src.suffix.lower()
    if ext in PHOTO_IN and not photos:
        return None
    if ext in VIDEO_IN and (not videos or not is_hevc(src)):
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if ext in PHOTO_IN:
        photo_to_jpg(src, dst)
    else:
        video_to_mp4(src, dst, cancel)
    from datetime import datetime
    from ..organizer.filetimes import set_file_times
    set_file_times(dst, datetime.fromtimestamp(src.stat().st_mtime))   # misma fecha que el original
    return out_rel


@dataclass
class CompatReport:
    created: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def convert_existing(manifest, videos: bool, progress: Callable[[int, int, str], None],
                     cancel: threading.Event, photos: bool = True, only: Optional[list[str]] = None) -> CompatReport:
    """Genera copias compatibles para lo ya copiado (todo el destino, o solo las rutas de `only`)."""
    rep = CompatReport()
    missing = compat_missing_libs(photos, videos)
    if missing:
        rep.failed.append(("(todas)", "faltan librerías: " + ", ".join(missing) +
                           ". Abrí la app con «Iniciar Cloudy.bat» para instalarlas."))
        return rep
    rels = only if only is not None else [f["local_path"] for f in manifest.all_files()]
    todo = [r for r in rels if compat_rel(r)]
    for i, rel in enumerate(todo, start=1):
        if cancel.is_set():
            rep.cancelled = True
            break
        progress(i, len(todo), PurePosixPath(rel).name)
        try:
            if make_compatible(manifest.root, rel, videos, cancel, photos=photos):
                rep.created += 1
            else:
                rep.skipped += 1
        except InterruptedError:
            rep.cancelled = True
            break
        except Exception as e:  # noqa: BLE001
            rep.failed.append((rel, str(e)[:200]))
    return rep
