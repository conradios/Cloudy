"""Detecta qué hay enchufado por cable: iPhone/iPad, celulares Android (MTP o ADB) y cámaras.

Prioridades:
  - iPhone/iPad: siempre por su vía propia (trae Live Photos, originales, ediciones, álbumes…).
  - Android con «Depuración USB» autorizada: ADB (además verifica el SHA-256 en el propio celular).
  - Android/cámara sin depuración: MTP (Windows), lo mismo que usa el Explorador. No hay que tocar nada.
Todo es por cable: los dispositivos por Wi-Fi que vea ADB se ignoran.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from .base import DeviceBackend

log = logging.getLogger(__name__)


@dataclass
class Found:
    kind: str            # "iphone" | "adb" | "mtp" | "adb-unauthorized"
    label: str
    detail: str
    make: Callable[[], DeviceBackend]

    @property
    def ready(self) -> bool:
        return self.kind != "adb-unauthorized"


def detect_devices(iphone_probe: Optional[DeviceBackend] = None, include_adb: bool = True) -> list[Found]:
    found: list[Found] = []

    # --- iPhone / iPad
    try:
        if iphone_probe is None:
            from .iphone import IPhoneBackend
            iphone_probe = IPhoneBackend()
        if iphone_probe.is_present():
            from .iphone import IPhoneBackend
            found.append(Found("iphone", "iPhone / iPad", "Por cable (con Live Photos, ediciones y álbumes)",
                               IPhoneBackend))
    except Exception as e:  # noqa: BLE001
        log.info("Sin soporte de iPhone: %s", type(e).__name__)

    # --- Android por ADB (si está instalado y el celular tiene «Depuración USB»)
    adb_ready = []
    if include_adb:
        try:
            from .adb import AdbBackend, find_adb, list_devices
            exe = find_adb()
            if exe:
                for d in list_devices(exe):
                    if d.state == "device":
                        adb_ready.append(d)
                        found.append(Found("adb", d.model or "Celular Android", "Android por cable (ADB, con "
                                           "verificación en el celular)",
                                           lambda s=d.serial, x=exe: AdbBackend(s, x)))
                    elif d.state == "unauthorized":
                        found.append(Found("adb-unauthorized", d.model or "Celular Android",
                                           "Falta aceptar «¿Permitir depuración USB?» en el celular",
                                           lambda s=d.serial, x=exe: AdbBackend(s, x)))
        except Exception as e:  # noqa: BLE001
            log.info("ADB no disponible: %s", type(e).__name__)

    # --- Android / cámaras por MTP (Windows)
    if sys.platform == "win32":
        try:
            from .mtp_windows import MtpWindowsBackend, list_devices as mtp_devices
            mtp = mtp_devices()
            # Un mismo celular con depuración USB aparece por las dos vías: si hay uno de cada, va por ADB.
            if not (len(mtp) == 1 and len(adb_ready) == 1):
                for d in mtp:
                    name = d["name"] or d["description"] or "Celular o cámara"
                    found.append(Found("mtp", name, "Por cable (Transferencia de archivos / MTP)",
                                       lambda p=d["pnp"], n=name: MtpWindowsBackend(p, n)))
        except Exception as e:  # noqa: BLE001
            log.info("MTP no disponible: %s", type(e).__name__)
    return found


def any_present(iphone_probe: Optional[DeviceBackend] = None) -> bool:
    """Consulta liviana para la conexión automática."""
    return any(f.ready for f in detect_devices(iphone_probe))
