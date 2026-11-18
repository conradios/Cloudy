"""Interfaz común de las fuentes de fotos: iPhone por cable, iCloud, y sus versiones simuladas.

Rutas del iPhone: relativas a /var/mobile/Media (lo que expone AFC), ej. "DCIM/100APPLE/IMG_0001.HEIC".
Rutas de iCloud: "icloud/<id-del-asset>/<versión>", ej. "icloud/AbC123/original".
La interfaz es de SOLO LECTURA. La única excepción es iCloud.delete_assets, que manda a
«Eliminado recientemente» (recuperable 30 días) y solo la usa «Liberar espacio».
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Iterator, Optional

from ..models import DeviceInfo, RemoteStat

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB, el máximo por lectura de AFC


class DeviceError(Exception):
    """Error con mensaje pensado para mostrarle al usuario."""


class DisconnectedError(DeviceError):
    """Se perdió la conexión en medio de una operación (cable, red, enlace vencido)."""


class DeviceBackend(ABC):
    source = "usb"
    #: cuánto esperar como máximo a que vuelva la conexión antes de rendirse
    recover_timeout = 30 * 60

    @abstractmethod
    def connect(self, on_status=None) -> DeviceInfo: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def listdir(self, path: str) -> list[str]: ...

    @abstractmethod
    def stat(self, path: str) -> Optional[RemoteStat]:
        """Devuelve None si la ruta no existe."""

    def stat_many(self, paths: list[str]) -> dict[str, Optional[RemoteStat]]:
        return {p: self.stat(p) for p in paths}

    @abstractmethod
    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        """Lee desde `offset` (para retomar un archivo cortado a la mitad)."""

    def read_all(self, path: str) -> bytes:
        return b"".join(self.read_chunks(path))

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    @property
    def is_demo(self) -> bool:
        return False

    # -------------------------------------------------------------- recuperación
    def is_connection_error(self, exc: BaseException) -> bool:
        """¿El error es por haber perdido la conexión (y vale la pena esperar y reintentar)?"""
        return isinstance(exc, (DisconnectedError, ConnectionError, TimeoutError, BrokenPipeError))

    def is_present(self) -> bool:
        """¿La fuente está disponible para reconectar? (iPhone enchufado / hay red)."""
        return True

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def recover(self, on_status: Callable[[str], None], cancel: threading.Event) -> bool:
        """Espera a que la fuente vuelva y reconecta. Devuelve False si se canceló o venció el tiempo."""
        deadline = time.monotonic() + self.recover_timeout
        announced = False
        while time.monotonic() < deadline and not cancel.is_set():
            try:
                if self.is_present():
                    self.reconnect()
                    on_status("Conexión recuperada: sigo donde había quedado.")
                    return True
            except Exception:  # noqa: BLE001
                pass
            if not announced:
                on_status(self.waiting_message())
                announced = True
            cancel.wait(3)
        return False

    def waiting_message(self) -> str:
        return "Se cortó la conexión. Esperando a que vuelva para seguir donde quedó…"
