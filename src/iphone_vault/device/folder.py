"""Fuente «carpeta»: tarjetas SD, cámaras de fotos, pendrives, discos o un celular que se ve como unidad.

Solo lectura. Nunca sigue accesos directos/enlaces que apunten fuera de la carpeta elegida.
Rutas internas: "carpeta/<id>/<ruta dentro de la carpeta>".
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..models import DeviceInfo, RemoteStat
from .android_common import join
from .base import CHUNK_SIZE, DeviceBackend, DeviceError, DisconnectedError


def _volume_id(path: Path) -> Optional[str]:
    """Número de serie del volumen en Windows (sigue igual aunque la tarjeta cambie de letra)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        root = os.path.splitdrive(str(path))[0] + "\\"
        serial = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial), None, None, None, 0)
        return f"{serial.value:08X}" if ok else None
    except Exception:  # noqa: BLE001
        return None


def folder_id(path: Path) -> str:
    resolved = path.resolve()
    vid = _volume_id(resolved)
    rel = os.path.splitdrive(str(resolved))[1] if vid else str(resolved)
    h = hashlib.sha256(f"{vid or ''}|{rel}".encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return f"{vid}-{h}" if vid else h


class FolderBackend(DeviceBackend):
    source = "usb"
    kind = "folder"
    recover_timeout = 10 * 60

    def __init__(self, root: str | os.PathLike, label: Optional[str] = None,
                 exclude: Optional[list[str | os.PathLike]] = None):
        self.root = Path(root)
        #: carpetas que nunca se leen (la carpeta destino de Cloudy, para no copiar lo ya copiado)
        self.exclude = [Path(e).resolve() for e in (exclude or [])]
        self.label = label or self.root.name or str(self.root)
        self.ns = f"carpeta/{folder_id(self.root)}"
        self._connected = False

    # ------------------------------------------------------------------ conexión
    def connect(self, on_status=None) -> DeviceInfo:
        if not self.root.is_dir():
            raise DeviceError(f"No encuentro la carpeta «{self.root}». ¿Está conectada la tarjeta o el disco?")
        self._connected = True
        return DeviceInfo(name=self.label, model="Carpeta, tarjeta SD o cámara", ios_version="",
                          udid=self.ns, connection="Carpeta", platform="", kind="folder")

    def close(self) -> None:
        self._connected = False

    def is_present(self) -> bool:
        return self.root.is_dir()

    def waiting_message(self) -> str:
        return "No encuentro la tarjeta o el disco. Volvé a conectarlo y sigo donde quedé…"

    # ------------------------------------------------------------------ rutas
    def _p(self, path: str) -> Path:
        if not self._connected:
            raise DisconnectedError("La carpeta no está conectada.")
        path = path.strip("/")
        if path != self.ns and not path.startswith(self.ns + "/"):
            raise PermissionError("ruta fuera de la carpeta elegida")
        rel = path[len(self.ns):].strip("/")
        parts = [s for s in rel.split("/") if s]
        if any(s in ("..", ".") or "\\" in s or ":" in s for s in parts):
            raise PermissionError("ruta no permitida")
        p = self.root.joinpath(*parts)
        try:
            real = p.resolve()
            root = self.root.resolve()
        except OSError as e:
            raise DisconnectedError(str(e)) from e
        if real != root and root not in real.parents:
            raise PermissionError("enlace que apunta fuera de la carpeta elegida")
        return p

    def _check_present(self, e: OSError):
        if not self.root.is_dir():
            raise DisconnectedError("Se desconectó la tarjeta o el disco.") from e

    # ------------------------------------------------------------------ lectura
    def listdir(self, path: str) -> list[str]:
        p = self._p(path)
        try:
            return sorted(os.listdir(p))
        except FileNotFoundError:
            self._check_present(FileNotFoundError())
            raise
        except OSError as e:
            self._check_present(e)
            raise

    def stat(self, path: str) -> Optional[RemoteStat]:
        try:
            p = self._p(path)
        except PermissionError:
            return None
        try:
            if p.is_symlink():
                return None           # no seguimos enlaces (evita bucles y salir de la carpeta)
            st = p.stat()
        except FileNotFoundError:
            return None
        except OSError as e:
            self._check_present(e)
            return None
        return RemoteStat(size=st.st_size, is_dir=p.is_dir(), mtime=datetime.fromtimestamp(st.st_mtime))

    def walk_files(self, top: str) -> Iterator[tuple[str, int, Optional[datetime]]]:
        from .android_common import MAX_DEPTH, skip_dir, skip_file
        base = self._p(top)
        stack = [(base, top.strip("/"), 0)]
        while stack:
            d, rel, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError as e:
                self._check_present(e)
                continue
            for e in sorted(entries, key=lambda x: x.name):
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if self._is_excluded(Path(e.path)):
                            continue
                        if depth < MAX_DEPTH + 4 and not skip_dir(e.name):
                            stack.append((Path(e.path), join(rel, e.name), depth + 1))
                    elif e.is_file(follow_symlinks=False) and not skip_file(e.name):
                        st = e.stat(follow_symlinks=False)
                        yield join(rel, e.name), st.st_size, datetime.fromtimestamp(st.st_mtime)
                except OSError:
                    continue

    def _is_excluded(self, d: Path) -> bool:
        """Nunca entrar en una carpeta destino de Cloudy (tiene .vault/manifest.db) ni en las excluidas."""
        try:
            if (d / ".vault" / "manifest.db").exists():
                return True
            r = d.resolve()
        except OSError:
            return True
        return any(r == x or x in r.parents for x in self.exclude)

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        p = self._p(path)
        try:
            with open(p, "rb") as fh:
                fh.seek(offset)
                while True:
                    data = fh.read(chunk_size)
                    if not data:
                        break
                    yield data
        except OSError as e:
            self._check_present(e)
            raise
