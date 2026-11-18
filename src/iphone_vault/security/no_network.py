"""Control de red del proceso: solo USB local (y, si activás iCloud, solo servidores de Apple).

Tres capas, para que ninguna librería (ni propia ni de terceros) pueda sacar datos:
  1. Hook de auditoría de Python (sys.addaudithook): intercepta en C toda conexión, envío UDP,
     resolución DNS y bind — incluso si alguien usa el módulo _socket directamente.
  2. Parches sobre socket/asyncio (cubren el event loop Proactor de Windows, que no pasa por el hook).
  3. Proxies desactivados: se ignoran HTTP(S)_PROXY y el proxy del sistema, para que nadie pueda
     «saltar» por un proxy local.

Reglas:
  - Sockets Unix: permitidos (usbmuxd en macOS/Linux).
  - Loopback (127.0.0.1 / ::1): SOLO al puerto de usbmuxd en Windows (27015) o a puertos que este
    mismo proceso abrió (pares de sockets internos de asyncio/Qt).
  - Con iCloud activado: además, IPs que salieron de resolver dominios de Apple.
  - Todo lo demás: NetworkBlockedError y queda registrado.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import sys
import threading

log = logging.getLogger(__name__)

APPLE_DOMAINS = (
    "icloud.com", "apple.com", "icloud-content.com", "apple-cloudkit.com", "cdn-apple.com",
    "icloud.com.cn", "apple.com.cn", "aaplimg.com",
)
USBMUXD_PORT = 27015
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "socks_proxy", "ws_proxy", "wss_proxy")

_installed = False
_icloud_allowed = False
_allow_phone_data = False      # el usuario aceptó explícitamente usar datos móviles para iCloud
_allowed_ips: set[str] = set()
_own_ports: set[int] = set()
_lock = threading.Lock()
blocked_attempts: list[str] = []


class NetworkBlockedError(ConnectionRefusedError):
    pass


def allow_icloud(enabled: bool) -> None:
    global _icloud_allowed
    _icloud_allowed = enabled
    if not enabled:
        with _lock:
            _allowed_ips.clear()


def icloud_allowed() -> bool:
    return _icloud_allowed


def allow_phone_data(enabled: bool) -> None:
    global _allow_phone_data
    _allow_phone_data = enabled


def _phone_data_blocked() -> bool:
    if _allow_phone_data:
        return False
    from .hotspot import using_phone_data
    return using_phone_data()


def _is_apple_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in APPLE_DOMAINS)


def _ip(host: str):
    try:
        return ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return None


def _block(what) -> None:
    msg = f"Conexión de red bloqueada hacia {what!r}"
    blocked_attempts.append(str(what))
    log.warning(msg)
    raise NetworkBlockedError(msg)


def _host_port(address):
    host, port = address[0], address[1] if len(address) > 1 else None
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    return str(host), port


def _check_address(address) -> None:
    if isinstance(address, (str, bytes)):
        return                              # socket Unix: local
    if not isinstance(address, tuple) or not address:
        _block(address)
    host, port = _host_port(address)
    ip = _ip(host)
    if host == "localhost" or (ip is not None and ip.is_loopback):
        if port == USBMUXD_PORT or port in _own_ports:
            return
        _block(address)
    if _icloud_allowed:
        if _phone_data_blocked():
            _block(f"{address} (la compu está usando los datos móviles del iPhone)")
        if ip is not None:
            with _lock:
                if str(ip) in _allowed_ips:
                    return
        elif _is_apple_host(host):
            return
    _block(address)


def _check_resolve(host) -> bool:
    """True si hay que registrar las IPs resultantes como permitidas."""
    if host is None:
        return False
    h = host.decode(errors="ignore") if isinstance(host, bytes) else str(host)
    ip = _ip(h)
    if h in ("localhost", "") or ip is not None:
        return False                        # resolver una IP no sale a la red; el connect se controla igual
    if _icloud_allowed and _is_apple_host(h):
        if _phone_data_blocked():
            _block(f"{h} (la compu está usando los datos móviles del iPhone)")
        return True
    _block(h)
    return False


def _audit(event: str, args) -> None:
    if event == "socket.connect":
        _check_address(args[1])
    elif event in ("socket.sendto", "socket.sendmsg"):
        if len(args) > 1 and args[1] is not None:
            _check_address(args[1])
    elif event == "socket.getaddrinfo":
        _check_resolve(args[0])
    elif event == "socket.gethostbyname" or event == "socket.gethostbyaddr":
        _check_resolve(args[0])
    elif event == "socket.bind":
        addr = args[1]
        if isinstance(addr, tuple) and len(addr) > 1:
            ip = _ip(_host_port(addr)[0])
            if ip is None or ip.is_loopback or ip.is_unspecified:
                with _lock:
                    _own_ports.add(int(addr[1]) if addr[1] else 0)


def _disable_proxies() -> None:
    for k in list(os.environ):
        if k.lower() in PROXY_VARS:
            del os.environ[k]
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    import urllib.request
    urllib.request.getproxies = lambda: {}
    try:
        import requests.compat
        import requests.utils
        requests.utils.getproxies = lambda: {}
        requests.compat.getproxies = lambda: {}
    except ImportError:
        pass


def install() -> None:
    """Instala el control. Idempotente."""
    global _installed
    if _installed:
        return
    _disable_proxies()

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_bind = socket.socket.bind
    orig_create_connection = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        record = _check_resolve(host)
        res = orig_getaddrinfo(host, *args, **kwargs)
        if record:
            with _lock:
                for r in res:
                    _allowed_ips.add(str(r[4][0]).split("%")[0])
        return res

    def connect(self, address):
        _check_address(address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        _check_address(address)
        return orig_connect_ex(self, address)

    def bind(self, address):
        res = orig_bind(self, address)
        try:
            name = self.getsockname()
            if isinstance(name, tuple) and len(name) > 1:
                with _lock:
                    _own_ports.add(int(name[1]))
        except OSError:
            pass
        return res

    def create_connection(address, *args, **kwargs):
        _check_address(address)
        return orig_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.bind = bind
    socket.create_connection = create_connection

    # asyncio en Windows (Proactor) no pasa por socket.connect ni por el hook: lo cubrimos acá.
    loop_classes = [asyncio.base_events.BaseEventLoop]
    try:
        from asyncio import proactor_events, selector_events
        loop_classes += [selector_events.BaseSelectorEventLoop, proactor_events.BaseProactorEventLoop]
    except ImportError:
        pass
    for cls in loop_classes:
        if "sock_connect" in cls.__dict__:
            orig = cls.__dict__["sock_connect"]

            async def sock_connect(self, sock, address, _orig=orig):
                _check_address(address)
                return await _orig(self, sock, address)

            cls.sock_connect = sock_connect
        if "create_connection" in cls.__dict__:
            orig_cc = cls.__dict__["create_connection"]

            async def create_conn(self, protocol_factory, host=None, port=None, *args, _orig=orig_cc, **kwargs):
                if host is not None:
                    _check_resolve(host) if _ip(str(host)) is None else _check_address((host, port))
                return await _orig(self, protocol_factory, host, port, *args, **kwargs)

            cls.create_connection = create_conn

    sys.addaudithook(_audit)   # no se puede quitar: queda activo toda la vida del proceso
    _installed = True
    log.info("Control de red activo: solo USB local (y Apple si activás iCloud). Proxies desactivados.")


def is_installed() -> bool:
    return _installed


def describe() -> str:
    if not _installed:
        return "Red sin controlar"
    return "Red: solo USB + Apple iCloud" if _icloud_allowed else "Red bloqueada: solo USB"
