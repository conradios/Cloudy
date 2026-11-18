"""Conexión real con el iPhone por cable USB usando pymobiledevice3 (servicio AFC).

pymobiledevice3 es asíncrono; acá lo envolvemos en un event loop propio que corre en un
hilo dedicado, así el resto de la app (escáner, transferencia, GUI) usa llamadas simples.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from typing import Iterator, Optional

from ..models import DeviceInfo, RemoteStat
from .base import CHUNK_SIZE, DeviceBackend, DeviceError, DisconnectedError

PAIR_WAIT_SECONDS = 90
STAT_CONCURRENCY = 32


def _abs(path: str) -> str:
    return "/" + path.lstrip("/")


class IPhoneBackend(DeviceBackend):
    source = "usb"

    def __init__(self, udid: Optional[str] = None):
        self._udid = udid
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="iphone-afc", daemon=True)
        self._thread.start()
        self._lockdown = None
        self._afc = None
        self.info: Optional[DeviceInfo] = None

    # ------------------------------------------------------------------ helpers
    def _run(self, coro, timeout: Optional[float] = 120):
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def _require(self):
        if self._afc is None:
            raise DisconnectedError("El iPhone no está conectado.")
        return self._afc

    # --------------------------------------------------------------- conexión
    def connect(self, on_status=None) -> DeviceInfo:
        from pymobiledevice3 import exceptions as exc
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        def status(msg: str):
            if on_status:
                on_status(msg)

        deadline = time.monotonic() + PAIR_WAIT_SECONDS
        last_hint = None
        status("Buscando el iPhone por cable… Si aparece «¿Confiar en esta computadora?», tocá «Confiar».")
        while True:
            try:
                # connection_type="USB": nunca usamos la conexión por Wi-Fi, solo el cable.
                # pair_timeout: espera a que toques «Confiar» en el iPhone (solo la primera vez).
                self._lockdown = self._run(
                    create_using_usbmux(serial=self._udid, connection_type="USB", autopair=True,
                                        pair_timeout=PAIR_WAIT_SECONDS),
                    timeout=PAIR_WAIT_SECONDS + 30,
                )
                break
            except exc.NoDeviceConnectedError:
                raise DeviceError(
                    "No encuentro ningún iPhone conectado por cable.\n"
                    "Revisá el cable, desbloqueá el iPhone y probá de nuevo."
                )
            except exc.ConnectionFailedToUsbmuxdError:
                raise DeviceError(
                    "No puedo hablar con el servicio de dispositivos de Apple.\n"
                    "En Windows instalá iTunes o la app «Dispositivos Apple» (Microsoft Store) "
                    "y volvé a abrir esta aplicación."
                )
            except (exc.PairingDialogResponsePendingError, exc.PasswordRequiredError,
                    exc.PasscodeRequiredError, exc.NotPairedError, TimeoutError) as e:
                hint = ("Desbloqueá el iPhone y tocá «Confiar» en el aviso que aparece en la pantalla."
                        if not isinstance(e, exc.PasswordRequiredError)
                        else "Desbloqueá el iPhone con tu código para continuar.")
                if hint != last_hint:
                    status(hint)
                    last_hint = hint
                if time.monotonic() > deadline:
                    raise DeviceError("No se confirmó la confianza en el iPhone a tiempo. " + hint)
                time.sleep(2)
            except exc.UserDeniedPairingError:
                raise DeviceError("En el iPhone se eligió «No confiar». Desconectá y conectá el cable para reintentar.")

        try:
            afc = AfcService(self._lockdown)
            self._afc = self._run(afc.__aenter__())
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"El iPhone está conectado pero no pude abrir el acceso a fotos (AFC): {e}")

        v = self._lockdown.all_values
        self.info = DeviceInfo(
            name=v.get("DeviceName") or "iPhone",
            model=self._lockdown.display_name or v.get("ProductType") or "iPhone",
            ios_version=v.get("ProductVersion") or "?",
            udid=v.get("UniqueDeviceID") or self._lockdown.identifier or "",
            connection="USB",
        )
        self._udid = self._udid or self.info.udid or None   # al reconectar, siempre el MISMO iPhone
        return self.info

    def close(self) -> None:
        try:
            if self._afc is not None:
                self._run(self._afc.aclose(), timeout=10)
            if self._lockdown is not None:
                self._run(self._lockdown.close(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._afc = None
            self._lockdown = None

    def shutdown(self) -> None:
        self.close()
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------ recuperación
    def is_present(self) -> bool:
        """¿Hay un iPhone enchufado por USB? (consulta rápida a usbmuxd, sin emparejar)."""
        from pymobiledevice3.usbmux import list_devices
        try:
            devices = self._run(list_devices(), timeout=10)
        except Exception:  # noqa: BLE001
            return False
        return any(d.is_usb and (self._udid is None or d.matches_udid(self._udid)) for d in devices)

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def is_connection_error(self, exc: BaseException) -> bool:
        if super().is_connection_error(exc):
            return True
        try:
            from pymobiledevice3 import exceptions as pexc
            return isinstance(exc, (pexc.ConnectionTerminatedError, pexc.NoDeviceConnectedError,
                                    pexc.ConnectionFailedError, pexc.NotConnectedError, OSError))
        except ImportError:
            return isinstance(exc, OSError)

    def waiting_message(self) -> str:
        return ("Se desconectó el iPhone. Volvé a conectarlo y desbloquealo: sigo exactamente donde quedé "
                "(no se pierde nada de lo ya copiado).")

    # --------------------------------------------------------- sistema de archivos
    def listdir(self, path: str) -> list[str]:
        afc = self._require()
        return self._run(afc.listdir(_abs(path)))

    async def _stat_async(self, afc, path: str) -> Optional[RemoteStat]:
        from pymobiledevice3.exceptions import AfcFileNotFoundError
        try:
            st = await afc.stat(_abs(path))
        except AfcFileNotFoundError:
            return None
        return RemoteStat(
            size=int(st.get("st_size", 0)),
            is_dir=st.get("st_ifmt") == "S_IFDIR",
            mtime=st.get("st_mtime"),
            birthtime=st.get("st_birthtime"),
        )

    def stat(self, path: str) -> Optional[RemoteStat]:
        return self._run(self._stat_async(self._require(), path))

    def stat_many(self, paths: list[str]) -> dict[str, Optional[RemoteStat]]:
        afc = self._require()

        async def run_all():
            sem = asyncio.Semaphore(STAT_CONCURRENCY)

            async def one(p):
                async with sem:
                    return p, await self._stat_async(afc, p)

            return dict(await asyncio.gather(*(one(p) for p in paths)))

        return self._run(run_all(), timeout=None)

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        afc = self._require()
        st = self.stat(path)
        if st is None:
            raise FileNotFoundError(path)
        handle = self._run(afc.fopen(_abs(path), "r"))
        try:
            if offset:
                self._run(afc.fseek(handle, offset))
            remaining = st.size - offset
            while remaining > 0:
                n = min(chunk_size, remaining)
                data = self._run(afc.fread(handle, n))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            try:
                self._run(afc.fclose(handle), timeout=10)
            except Exception:  # noqa: BLE001
                pass
