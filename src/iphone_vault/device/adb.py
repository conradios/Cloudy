"""Android por cable con ADB (las herramientas oficiales de Google, «platform-tools»).

Sirve en Windows, macOS y Linux, para cualquier marca (Samsung, Motorola, Xiaomi, Pixel…).
Requiere activar «Depuración USB» en el celular y aceptar en pantalla la huella de ESTA compu.

Seguridad:
  - SOLO dispositivos conectados por USB: se rechaza todo lo que ADB vea por Wi-Fi (ip:puerto),
    emuladores o conexiones mDNS. Se fuerza ADB_MDNS=0 y el servidor de ADB en 127.0.0.1.
  - Solo comandos de LECTURA en el celular: ls, find, stat, cat, dd (leer), sha256sum, getprop.
    No hay ninguna operación para borrar, mover ni escribir.
  - Nada de shell en la compu: subprocess con lista de argumentos. En el celular, cada ruta va entre
    comillas simples escapadas y después de «--», y tiene que estar dentro de /sdcard o /storage/<id>.
  - La ruta de adb sale de lugares conocidos (carpeta de Cloudy, winget, Android SDK, Homebrew o PATH).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..config import config_dir
from ..models import DeviceInfo, RemoteStat
from .android_common import INTERNAL, join, safe_serial
from .base import CHUNK_SIZE, DeviceBackend, DeviceError, DisconnectedError

log = logging.getLogger(__name__)

INTERNAL_ROOT = "/sdcard"
_VOLUME = re.compile(r"^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$|^[0-9A-Fa-f]{8,32}$")
_SERIAL_NET = re.compile(r"(:\d+$)|(^emulator-)|(_adb-tls-)|(\._tcp)")
_DD_BLOCK = 64 * 1024


class AdbMissing(DeviceError):
    pass


class AdbUnauthorized(DeviceError):
    pass


# --------------------------------------------------------------------------- dónde está adb
def _candidates() -> list[Path]:
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    c = [config_dir() / "platform-tools" / exe]
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if local:
            c.append(local / "Android" / "Sdk" / "platform-tools" / exe)
            pk = local / "Microsoft" / "WinGet" / "Packages"
            if pk.is_dir():
                c += sorted(pk.glob("Google.PlatformTools_*/platform-tools/" + exe))
            c.append(local / "Microsoft" / "WinGet" / "Links" / exe)
    else:
        home = Path.home()
        c += [Path("/opt/homebrew/bin/adb"), Path("/usr/local/bin/adb"),
              home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
              home / "Android" / "Sdk" / "platform-tools" / "adb", Path("/usr/bin/adb")]
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            c.append(Path(os.environ[var]) / "platform-tools" / exe)
    w = shutil.which("adb")
    if w:
        c.append(Path(w))
    return c


def find_adb() -> Optional[Path]:
    for p in _candidates():
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def adb_env() -> dict:
    env = dict(os.environ)
    env.update({"ADB_MDNS": "0", "ADB_MDNS_AUTO_CONNECT": "0", "ADB_EMU": "0",
                "ADB_SERVER_SOCKET": "tcp:127.0.0.1:5037", "ANDROID_ADB_SERVER_ADDRESS": "127.0.0.1"})
    for k in ("ADB_VENDOR_KEYS", "ANDROID_SERIAL"):
        env.pop(k, None)
    return env


_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sh_quote(s: str) -> str:
    """Comillas simples para el sh del celular (a prueba de espacios, $, `, comillas, ;, |, saltos…)."""
    if "\x00" in s:
        raise PermissionError("ruta inválida")
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- listado de aparatos
class AdbDevice:
    def __init__(self, serial: str, state: str, model: str = "", usb: bool = False):
        self.serial, self.state, self.model, self.usb = serial, state, model, usb

    def __repr__(self):
        return f"AdbDevice({self.serial!r}, {self.state!r}, {self.model!r}, usb={self.usb})"


def parse_devices(out: str) -> list[AdbDevice]:
    devs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*", "adb server")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        attrs = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        usb = "usb" in attrs and not _SERIAL_NET.search(serial)
        devs.append(AdbDevice(serial, state, attrs.get("model", "").replace("_", " "), usb))
    return devs


def list_devices(adb: Optional[Path] = None, timeout: float = 15) -> list[AdbDevice]:
    adb = adb or find_adb()
    if adb is None:
        return []
    try:
        r = subprocess.run([str(adb), "devices", "-l"], capture_output=True, text=True, timeout=timeout,  # nosec B603
                           env=adb_env(), creationflags=_NOWINDOW, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [d for d in parse_devices(r.stdout) if d.usb]


# --------------------------------------------------------------------------- backend
class AdbBackend(DeviceBackend):
    source = "usb"
    kind = "android"
    recover_timeout = 30 * 60

    def __init__(self, serial: Optional[str] = None, adb: Optional[Path] = None):
        self.adb = adb or find_adb()
        self.serial = serial
        self.ns = ""
        self._storages: dict[str, str] = {}     # slug → raíz en el celular
        self._lock = threading.Lock()
        self._has_sha = None

    # ------------------------------------------------------------------ comandos
    def _base(self) -> list[str]:
        if self.adb is None:
            raise AdbMissing("Falta ADB (las herramientas de Android de Google).")
        b = [str(self.adb)]
        if self.serial:
            b += ["-s", self.serial]
        return b

    def _run(self, args: list[str], timeout: float = 120, binary: bool = False):
        try:
            r = subprocess.run(self._base() + args, capture_output=True, timeout=timeout,  # nosec B603
                               env=adb_env(), creationflags=_NOWINDOW)
        except subprocess.TimeoutExpired as e:
            raise DisconnectedError("El celular no responde.") from e
        except OSError as e:
            raise AdbMissing(f"No se pudo ejecutar ADB: {e}") from e
        err = r.stderr.decode("utf-8", "replace")
        if r.returncode != 0 and re.search(r"device .*not found|no devices|device offline|closed|"
                                           r"unauthorized|protocol fault", err, re.I):
            if "unauthorized" in err.lower():
                raise AdbUnauthorized(_UNAUTH_MSG)
            raise DisconnectedError("Se desconectó el celular.")
        return r if binary else _Text(r)

    def shell(self, cmd: str, timeout: float = 120) -> "_Text":
        # exec-out: salida cruda, sin terminal (no cambia \n por \r\n ni toca bytes)
        return self._run(["exec-out", cmd], timeout=timeout)

    # ------------------------------------------------------------------ conexión
    def connect(self, on_status=None) -> DeviceInfo:
        if self.adb is None:
            raise AdbMissing("Falta ADB (las herramientas de Android de Google).")
        if on_status:
            on_status("Buscando el celular Android por cable…")
        devs = list_devices(self.adb)
        if self.serial:
            devs = [d for d in devs if d.serial == self.serial]
        if not devs:
            raise DeviceError(_NOT_FOUND_MSG)
        ready = [d for d in devs if d.state == "device"]
        if not ready:
            if any(d.state == "unauthorized" for d in devs):
                raise AdbUnauthorized(_UNAUTH_MSG)
            raise DeviceError("El celular está conectado pero no listo. Desenchufalo y volvé a enchufarlo.")
        if len(ready) > 1 and not self.serial:
            raise DeviceError("Hay más de un celular Android conectado: dejá enchufado solo uno.")
        d = ready[0]
        self.serial = d.serial
        props = self._props()
        serial = props.get("ro.serialno") or d.serial
        self.ns = f"android/{safe_serial(serial)}"
        self._storages = self._find_storages()
        brand = props.get("ro.product.brand", "").capitalize()
        model = props.get("ro.product.model", d.model) or "Android"
        name = props.get("persist.sys.device_name") or props.get("ro.product.marketname") or \
            (f"{brand} {model}".strip() if brand.lower() not in model.lower() else model)
        return DeviceInfo(name=name[:60], model=model[:60], ios_version=props.get("ro.build.version.release", ""),
                          udid=serial, connection="USB (ADB)", platform="Android", kind="android")

    def _props(self) -> dict:
        r = self.shell("getprop", timeout=30)
        out = {}
        for m in re.finditer(r"^\[([^\]]+)\]: \[([^\]]*)\]$", r.text, re.M):
            out[m.group(1)] = m.group(2)
        return out

    def _find_storages(self) -> dict[str, str]:
        st = {INTERNAL: INTERNAL_ROOT}
        r = self.shell("ls -1 /storage 2>/dev/null", timeout=30)
        for name in r.text.split():
            if _VOLUME.match(name):
                st[f"sd-{name.replace('-', '').upper()}"] = f"/storage/{name}"
        return st

    def close(self) -> None:
        pass

    def is_present(self) -> bool:
        try:
            return any(d.serial == self.serial and d.state == "device" for d in list_devices(self.adb))
        except Exception:  # noqa: BLE001
            return False

    def waiting_message(self) -> str:
        return "Se desconectó el celular. Volvé a enchufarlo (y desbloquealo) y sigo donde quedé…"

    # ------------------------------------------------------------------ rutas
    def _remote(self, path: str) -> str:
        """Ruta de Cloudy → ruta real en el celular, validada."""
        path = path.strip("/")
        if not self.ns or (path != self.ns and not path.startswith(self.ns + "/")):
            raise PermissionError("ruta fuera del celular conectado")
        rest = path[len(self.ns):].strip("/")
        if not rest:
            return ""
        slug, _, rel = rest.partition("/")
        root = self._storages.get(slug)
        if root is None:
            raise FileNotFoundError(path)
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise PermissionError("ruta no permitida")
        return root + ("/" + "/".join(parts) if parts else "")

    def _cloudy(self, slug: str, root: str, remote: str) -> str:
        return join(self.ns, slug, remote[len(root):])

    # ------------------------------------------------------------------ lectura
    def listdir(self, path: str) -> list[str]:
        remote = self._remote(path)
        if remote == "":
            return sorted(self._storages)
        r = self._run(["exec-out", f"ls -1a -- {sh_quote(remote)} 2>/dev/null"], binary=True)
        names = r.stdout.decode("utf-8", "surrogateescape").replace("\r\n", "\n").split("\n")
        return sorted(n for n in names if n and n not in (".", ".."))

    def stat(self, path: str) -> Optional[RemoteStat]:
        try:
            remote = self._remote(path)
        except FileNotFoundError:
            return None
        if remote == "":
            return RemoteStat(size=0, is_dir=True)
        r = self.shell(f"stat -c '%s %Y %F' -- {sh_quote(remote)} 2>/dev/null", timeout=30)
        m = re.match(r"(\d+) (\d+) (.+)", r.text.strip())
        if not m:
            return None
        return RemoteStat(size=int(m.group(1)), is_dir="directory" in m.group(3),
                          mtime=datetime.fromtimestamp(int(m.group(2))))

    def walk_files(self, top: str) -> Iterator[tuple[str, int, Optional[datetime]]]:
        """Atajo: un solo `find` lista todo (miles de archivos en segundos)."""
        remote = self._remote(top)
        slug = top.strip("/")[len(self.ns):].strip("/").split("/", 1)[0]
        root = self._storages[slug]
        q = sh_quote(remote)
        names_raw = self._run(["exec-out", f"find {q} -type f -print0 2>/dev/null"], timeout=600, binary=True).stdout
        names = [n.decode("utf-8", "surrogateescape") for n in names_raw.split(b"\0") if n]
        if not names:
            return
        known = set(names)
        out = self._run(["exec-out", f"find {q} -type f -exec stat -c '%s %Y %n' {{}} + 2>/dev/null"],
                        timeout=900, binary=True).stdout.decode("utf-8", "surrogateescape")
        info: dict[str, tuple[int, int]] = {}
        lines = out.split("\n")
        i = 0
        while i < len(lines):
            m = re.match(r"^(\d+) (\d+) (.*)$", lines[i].rstrip("\r"))
            i += 1
            if not m:
                continue
            name = m.group(3)
            # un nombre con salto de línea ocupa varias líneas: se reconstruye contra la lista exacta
            while name not in known and i < len(lines) and not re.match(r"^\d+ \d+ /", lines[i]):
                name += "\n" + lines[i]
                i += 1
            if name in known:
                info[name] = (int(m.group(1)), int(m.group(2)))
        for n in names:
            if n not in info:
                st = self.stat(self._cloudy(slug, root, n))
                if st is None or st.is_dir:
                    continue
                info[n] = (st.size, int(st.mtime.timestamp()) if st.mtime else 0)
            size, mt = info[n]
            yield self._cloudy(slug, root, n), size, (datetime.fromtimestamp(mt) if mt else None)

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        remote = self._remote(path)
        st = self.stat(path)
        if st is None or st.is_dir:
            if not self.is_present():
                raise DisconnectedError("Se desconectó el celular.")
            raise FileNotFoundError(path)
        expected = st.size - offset
        skip_blocks, discard = divmod(offset, _DD_BLOCK)
        if offset:
            cmd = f"dd if={sh_quote(remote)} bs={_DD_BLOCK} skip={skip_blocks} 2>/dev/null"
        else:
            cmd = f"cat -- {sh_quote(remote)}"
        proc = subprocess.Popen(self._base() + ["exec-out", cmd], stdout=subprocess.PIPE,  # nosec B603
                                stderr=subprocess.DEVNULL, env=adb_env(), creationflags=_NOWINDOW)
        served = 0
        try:
            if proc.stdout is None:
                raise DeviceError("No se pudo leer del celular.")
            while served < expected:
                data = proc.stdout.read(min(chunk_size, 1024 * 1024))
                if not data:
                    break
                if discard:
                    cut = min(discard, len(data))
                    data, discard = data[cut:], discard - cut
                    if not data:
                        continue
                served += len(data)
                yield data
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        if served < expected:
            if not self.is_present():
                raise DisconnectedError("Se desconectó el celular.")
            raise DeviceError(f"El celular entregó el archivo incompleto ({served} de {expected} bytes).")

    def remote_sha256(self, path: str) -> Optional[str]:
        """SHA-256 calculado en el propio celular: prueba de punta a punta de que la copia es idéntica."""
        if self._has_sha is False:
            return None
        r = self.shell(f"sha256sum -- {sh_quote(self._remote(path))} 2>/dev/null", timeout=900)
        m = re.match(r"^\\?([0-9a-f]{64})\b", r.text.strip())   # «\» inicial: nombre con caracteres raros
        if not m:
            self._has_sha = False if self._has_sha is None else self._has_sha
            return None
        self._has_sha = True
        return m.group(1)


class _Text:
    def __init__(self, r):
        self.returncode = r.returncode
        self.text = r.stdout.decode("utf-8", "replace")


_NOT_FOUND_MSG = ("No encuentro un celular Android por cable con «Depuración USB». Revisá que esté enchufado "
                  "y desbloqueado.")
_UNAUTH_MSG = ("Mirá la pantalla del celular: aceptá «¿Permitir depuración USB?» (podés tildar «Permitir "
               "siempre desde esta computadora»).")
