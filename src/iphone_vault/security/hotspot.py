"""Detecta si la compu está saliendo a internet a través del iPhone (Punto de acceso personal).

Cuando el iPhone está enchufado por USB y tiene activado «Punto de acceso personal», Windows lo
ve como una placa de red «Ethernet» (Apple Mobile Device Ethernet) y puede mandar TODO el tráfico
de la compu por los datos móviles del teléfono. Lo mismo pasa si la compu se conecta al hotspot por Wi-Fi
o Bluetooth.

El iPhone siempre reparte direcciones de la red 172.20.10.0/28 en su hotspot, así que si alguna
placa de red de la compu tiene una IP ahí, la compu está usando el teléfono como módem.

La copia POR CABLE no usa internet (es el protocolo USB de Apple), así que no se ve afectada.
Lo que sí se bloquea por defecto es iCloud, para no gastar datos móviles.
"""
from __future__ import annotations

import ipaddress
import time

IPHONE_HOTSPOT_NET = ipaddress.ip_network("172.20.10.0/28")
# Android: redes clásicas de «Anclaje USB» (42), «Zona Wi-Fi» (43) y Bluetooth (44). Desde Android 11 algunas
# marcas eligen la red al azar: la detección es «mejor esfuerzo». La copia por cable no se ve afectada nunca.
ANDROID_HOTSPOT_NETS = [ipaddress.ip_network(n) for n in ("192.168.42.0/24", "192.168.43.0/24", "192.168.44.0/24")]
PHONE_NETS = [IPHONE_HOTSPOT_NET, *ANDROID_HOTSPOT_NETS]
_cache: tuple[float, list[str]] = (0.0, [])


def hotspot_interfaces(max_age: float = 3.0) -> list[str]:
    """Nombres de las conexiones de red que están usando el hotspot del iPhone (lista vacía = ninguna)."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < max_age:
        return _cache[1]
    found: list[str] = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                try:
                    ip = ipaddress.ip_address(a.address.split("%")[0])
                    if any(ip in n for n in PHONE_NETS if n.version == ip.version):
                        found.append(name)
                        break
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        found = []
    _cache = (now, found)
    return found


def using_phone_data() -> bool:
    return bool(hotspot_interfaces())


MESSAGE = ("Tu compu está conectada a internet a través del celular (Punto de acceso / Anclaje USB): eso gasta DATOS "
           "MÓVILES. La copia por cable no usa internet y sigue funcionando, pero iCloud queda bloqueado para "
           "no consumir datos. Para que no pase: en el iPhone, Ajustes > Punto de acceso personal > desactivá "
           "«Permitir que otros se conecten»; en Android, Ajustes > Conexiones > Anclaje a red > desactivá "
           "«Anclaje USB» y «Zona Wi-Fi» (y conectá la compu a tu Wi-Fi o cable de casa).")
