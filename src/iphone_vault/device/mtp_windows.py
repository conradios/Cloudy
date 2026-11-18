"""Android (y cámaras) por cable en Windows, con MTP — lo mismo que usa el Explorador de Windows.

No hay que instalar nada en el celular ni activar opciones de desarrollador: alcanza con enchufarlo,
desbloquearlo y elegir «Transferencia de archivos» en el aviso de USB.

Usa la API oficial de Windows (Windows Portable Devices) a través de comtypes.
Seguridad: el aparato se abre con acceso de SOLO LECTURA (GENERIC_READ). Esta clase no tiene ninguna
operación para borrar, mover ni escribir en el celular. Todo pasa por el cable: MTP no usa la red.
Todas las llamadas a la API van en UN hilo propio (WPD es COM y no le gusta cambiar de hilo).
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import queue
import sys
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Optional

from ..models import DeviceInfo, RemoteStat
from .android_common import INTERNAL, join, safe_serial, storage_slug
from .base import CHUNK_SIZE, DeviceBackend, DeviceError, DisconnectedError

log = logging.getLogger(__name__)

GENERIC_READ = 0x80000000
SECURITY_IMPERSONATION = 0x00020000
STGM_READ = 0

FMT_OBJ = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"       # WPD_OBJECT_PROPERTIES_V1
FMT_CLIENT = "{204D9F0C-2292-4080-9F42-40664E70F859}"    # WPD_CLIENT_INFORMATION_PROPERTIES_V1
FMT_DEVICE = "{26D4979A-E643-4626-9E2B-736DC0C92FDC}"    # WPD_DEVICE_PROPERTIES_V1
FMT_STORAGE = "{01A3057A-74D6-4E80-BEA7-DC4C212CE50A}"   # WPD_STORAGE_OBJECT_PROPERTIES_V1
FMT_RESOURCE = "{E81E79BE-34F0-41BF-B53F-F1A06AE87842}"  # WPD_RESOURCE_DEFAULT
CT_FOLDER = "{27E2E392-A111-48E0-AB0C-E17705A05F85}"
CT_FUNCTIONAL = "{99ED0160-17FF-4C44-9D98-1D7A6F941921}"

P_ID, P_PARENT, P_NAME, P_PUID, P_FORMAT, P_CONTENT_TYPE = 2, 3, 4, 5, 6, 7
P_SIZE, P_ORIGINAL_NAME, P_DATE_CREATED, P_DATE_MODIFIED = 11, 12, 18, 19
D_MANUFACTURER, D_MODEL, D_SERIAL, D_FIRMWARE, D_FRIENDLY = 7, 8, 9, 3, 12
S_TYPE, S_SERIAL = 2, 8
STORAGE_REMOVABLE = {2, 4}

VT_DATE, VT_FILETIME = 7, 64
_GETTERS = ("GetStringValue", "GetUnsignedLargeIntegerValue", "GetUnsignedIntegerValue", "GetGuidValue", "_date")
_TYPES = {(FMT_OBJ, P_SIZE): "GetUnsignedLargeIntegerValue", (FMT_OBJ, P_CONTENT_TYPE): "GetGuidValue",
          (FMT_OBJ, P_DATE_MODIFIED): "_date", (FMT_OBJ, P_DATE_CREATED): "_date",
          (FMT_STORAGE, S_TYPE): "GetUnsignedIntegerValue"}
_OLE_EPOCH = datetime(1899, 12, 30)


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import comtypes  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- hilo COM
class _ComThread:
    """Ejecuta funciones en un único hilo con COM inicializado."""

    def __init__(self):
        self._q: "queue.Queue[tuple[Callable, tuple, queue.Queue] | None]" = queue.Queue()
        self._t = threading.Thread(target=self._loop, name="Cloudy-MTP", daemon=True)
        self._t.start()

    def _loop(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while True:
                job = self._q.get()
                if job is None:
                    break
                fn, args, out = job
                try:
                    out.put((True, fn(*args)))
                except BaseException as e:  # noqa: BLE001
                    out.put((False, e))
        finally:
            comtypes.CoUninitialize()

    def call(self, fn: Callable, *args, timeout: float = 600) -> Any:
        out: queue.Queue = queue.Queue(maxsize=1)
        self._q.put((fn, args, out))
        try:
            ok, val = out.get(timeout=timeout)
        except queue.Empty:
            raise DisconnectedError("El celular no responde (MTP).") from None
        if not ok:
            raise val
        return val

    def stop(self):
        self._q.put(None)


# --------------------------------------------------------------------------- API WPD (en el hilo COM)
class _Wpd:
    def __init__(self):
        import comtypes
        import comtypes.client as cc
        cc.GetModule("portabledeviceapi.dll")
        cc.GetModule("portabledevicetypes.dll")
        import comtypes.gen.PortableDeviceApiLib as port  # type: ignore
        import comtypes.gen.PortableDeviceTypesLib as types  # type: ignore
        self.comtypes, self.cc, self.port, self.types = comtypes, cc, port, types
        self.CLSCTX = comtypes.CLSCTX_INPROC_SERVER
        self.mgr = cc.CreateObject(port.PortableDeviceManager, clsctx=self.CLSCTX,
                                   interface=port.IPortableDeviceManager)

    def key(self, fmtid: str, pid: int):
        k = self.port._tagpropertykey()
        k.fmtid = self.comtypes.GUID(fmtid)
        k.pid = pid
        return ctypes.pointer(k)

    # -- aparatos
    def devices(self) -> list[dict]:
        try:
            self.mgr.RefreshDeviceList()
        except Exception:  # noqa: BLE001
            pass
        count = ctypes.pointer(ctypes.c_ulong(0))
        self.mgr.GetDevices(ctypes.POINTER(ctypes.c_wchar_p)(), count)
        n = count.contents.value
        if n == 0:
            return []
        ids = (ctypes.c_wchar_p * n)()
        self.mgr.GetDevices(ctypes.cast(ids, ctypes.POINTER(ctypes.c_wchar_p)), count)
        out = []
        for i in range(count.contents.value):
            pnp = ids[i]
            out.append({"pnp": pnp, "name": self._mgr_str(self.mgr.GetDeviceFriendlyName, pnp),
                        "manufacturer": self._mgr_str(self.mgr.GetDeviceManufacturer, pnp),
                        "description": self._mgr_str(self.mgr.GetDeviceDescription, pnp)})
        return out

    @staticmethod
    def _mgr_str(fn, pnp: str) -> str:
        try:
            size = ctypes.pointer(ctypes.c_ulong(0))
            fn(pnp, ctypes.POINTER(ctypes.c_ushort)(), size)
            if size.contents.value == 0:
                return ""
            buf = ctypes.create_unicode_buffer(size.contents.value)
            fn(pnp, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)), size)
            return buf.value
        except Exception:  # noqa: BLE001
            return ""

    def open(self, pnp: str):
        port = self.port
        info = self.cc.CreateObject(self.types.PortableDeviceValues, clsctx=self.CLSCTX,
                                    interface=port.IPortableDeviceValues)
        info.SetStringValue(self.key(FMT_CLIENT, 2), "Cloudy")
        info.SetUnsignedIntegerValue(self.key(FMT_CLIENT, 3), 1)
        info.SetUnsignedIntegerValue(self.key(FMT_CLIENT, 4), 0)
        info.SetUnsignedIntegerValue(self.key(FMT_CLIENT, 5), 0)
        info.SetUnsignedIntegerValue(self.key(FMT_CLIENT, 8), SECURITY_IMPERSONATION)
        info.SetUnsignedIntegerValue(self.key(FMT_CLIENT, 9), GENERIC_READ)     # SOLO LECTURA
        dev = self.cc.CreateObject(port.PortableDevice, clsctx=self.CLSCTX, interface=port.IPortableDevice)
        dev.Open(pnp, info)
        content = dev.Content()
        return dev, content, content.Properties(), content.Transfer()

    def children(self, content, parent: str) -> list[str]:
        enum = content.EnumObjects(ctypes.c_ulong(0), parent, ctypes.POINTER(self.port.IPortableDeviceValues)())
        out = []
        while True:
            want = 64
            arr = (ctypes.c_wchar_p * want)()
            got = ctypes.pointer(ctypes.c_ulong(0))
            enum.Next(want, ctypes.cast(arr, ctypes.POINTER(ctypes.c_wchar_p)), got)
            n = got.contents.value
            if n == 0:
                break
            out += [arr[i] for i in range(n)]
            if n < want:
                break
        return out

    def props(self, properties, obj: str, wanted: list[tuple[str, int]]) -> dict:
        keys = self.cc.CreateObject(self.types.PortableDeviceKeyCollection, clsctx=self.CLSCTX,
                                    interface=self.port.IPortableDeviceKeyCollection)
        for fmt, pid in wanted:
            keys.Add(self.key(fmt, pid))
        vals = properties.GetValues(obj, keys)
        out: dict = {}
        for fmt, pid in wanted:
            k = self.key(fmt, pid)
            first = _TYPES.get((fmt, pid), "GetStringValue")
            order = [first] + [g for g in _GETTERS if g != first]
            for getter in order:
                try:
                    v = self._date(vals, k) if getter == "_date" else getattr(vals, getter)(k)
                except Exception:  # noqa: BLE001
                    continue
                if v is None:
                    continue
                if first in ("GetUnsignedLargeIntegerValue", "GetUnsignedIntegerValue") and isinstance(v, str):
                    v = int(v) if v.strip().isdigit() else None
                    if v is None:
                        continue
                if first == "GetGuidValue":
                    v = str(v).upper()
                out[(fmt, pid)] = v
                break
        return out

    @staticmethod
    def _date(vals, k) -> Optional[datetime]:
        pv = vals.GetValue(k)
        addr = ctypes.addressof(pv)
        vt = ctypes.c_ushort.from_address(addr).value
        if vt == VT_DATE:
            return _OLE_EPOCH + timedelta(days=ctypes.c_double.from_address(addr + 8).value)
        if vt == VT_FILETIME:
            ft = ctypes.c_uint64.from_address(addr + 8).value
            return datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
        return None

    def open_stream(self, transfer, obj: str):
        optimal = ctypes.pointer(ctypes.c_ulong(0))
        p_stream = ctypes.POINTER(self.port.IStream)()
        optimal, p_stream = transfer.GetStream(obj, self.key(FMT_RESOURCE, 0), STGM_READ, optimal, p_stream)
        block = optimal.contents.value or 256 * 1024
        return p_stream.value, max(64 * 1024, min(block, 4 * 1024 * 1024))

    @staticmethod
    def read(stream, block: int) -> bytes:
        buf = (ctypes.c_ubyte * block)()
        buf, n = stream.RemoteRead(buf, ctypes.c_ulong(block))
        return bytes(bytearray(buf)[:n])


# --------------------------------------------------------------------------- listado de aparatos
_com_singleton: Optional[_ComThread] = None
_wpd_singleton: Optional[_Wpd] = None
_single_lock = threading.Lock()


def _com() -> _ComThread:
    global _com_singleton
    with _single_lock:
        if _com_singleton is None:
            _com_singleton = _ComThread()
        return _com_singleton


def _wpd() -> _Wpd:
    global _wpd_singleton
    if _wpd_singleton is None:
        _wpd_singleton = _Wpd()
    return _wpd_singleton


def list_devices() -> list[dict]:
    """Aparatos MTP/PTP conectados (sin iPhone: esos van por su propia vía, que trae más datos)."""
    if not available():
        return []
    try:
        devs = _com().call(lambda: _wpd().devices(), timeout=60)
    except Exception as e:  # noqa: BLE001
        log.info("WPD no disponible: %s", type(e).__name__)
        return []
    return [d for d in devs if "apple" not in (d["manufacturer"] + d["name"]).lower()]


# --------------------------------------------------------------------------- backend
class MtpWindowsBackend(DeviceBackend):
    source = "usb"
    kind = "android"
    recover_timeout = 30 * 60

    def __init__(self, pnp_id: Optional[str] = None, name_hint: str = ""):
        self.pnp_id = pnp_id
        self.name_hint = name_hint
        self.ns = ""
        self._dev = self._content = self._props = self._transfer = None
        self._storages: dict[str, str] = {}        # slug → objectId
        self._ids: dict[str, str] = {}             # ruta de Cloudy → objectId
        self._meta: dict[str, RemoteStat] = {}
        self._kids: dict[str, list[str]] = {}       # ruta → nombres hijos

    # ------------------------------------------------------------------ conexión
    def connect(self, on_status=None) -> DeviceInfo:
        if not available():
            raise DeviceError("La conexión MTP solo está disponible en Windows.")
        if on_status:
            on_status("Abriendo el celular (Transferencia de archivos)…")
        if not self.pnp_id:
            devs = list_devices()
            if not devs:
                raise DeviceError("No encuentro el celular. Enchufalo, desbloquealo y elegí «Transferencia de "
                                  "archivos» en el aviso de USB.")
            if len(devs) > 1:
                raise DeviceError("Hay más de un aparato conectado: dejá enchufado solo uno.")
            self.pnp_id, self.name_hint = devs[0]["pnp"], devs[0]["name"]
        return _com().call(self._connect_com)

    def _connect_com(self) -> DeviceInfo:
        w = _wpd()
        try:
            self._dev, self._content, self._props, self._transfer = w.open(self.pnp_id)
        except Exception as e:  # noqa: BLE001
            raise DeviceError("No pude abrir el celular. Desbloquealo y elegí «Transferencia de archivos» "
                              "en el aviso de USB.") from e
        d = w.props(self._props, "DEVICE", [(FMT_DEVICE, D_MANUFACTURER), (FMT_DEVICE, D_MODEL),
                                            (FMT_DEVICE, D_SERIAL), (FMT_DEVICE, D_FIRMWARE),
                                            (FMT_DEVICE, D_FRIENDLY)])
        serial = str(d.get((FMT_DEVICE, D_SERIAL)) or "")
        if not serial:
            serial = "pnp-" + hashlib.sha256(self.pnp_id.lower().encode()).hexdigest()[:12]
        self.ns = f"android/{safe_serial(serial)}"
        self._load_storages()
        maker = str(d.get((FMT_DEVICE, D_MANUFACTURER)) or "")
        model = str(d.get((FMT_DEVICE, D_MODEL)) or self.name_hint or "Android")
        name = str(d.get((FMT_DEVICE, D_FRIENDLY)) or self.name_hint or model)
        if maker and maker.lower() not in model.lower() and maker.lower() not in name.lower():
            model = f"{maker.capitalize()} {model}"
        return DeviceInfo(name=name[:60], model=model[:60], ios_version="", udid=serial,
                          connection="USB (MTP)", platform="Android", kind="android")

    def _load_storages(self):
        w = _wpd()
        self._storages.clear()
        self._ids.clear()
        self._meta.clear()
        self._kids.clear()
        objs = w.children(self._content, "DEVICE")
        for i, obj in enumerate(objs):
            p = w.props(self._props, obj, [(FMT_OBJ, P_NAME), (FMT_STORAGE, S_TYPE), (FMT_STORAGE, S_SERIAL)])
            stype = p.get((FMT_STORAGE, S_TYPE))
            removable = (stype in STORAGE_REMOVABLE) if isinstance(stype, int) else None
            slug = storage_slug(str(p.get((FMT_OBJ, P_NAME)) or ""), i, removable,
                                str(p.get((FMT_STORAGE, S_SERIAL)) or ""))
            while slug in self._storages:
                slug += "_"
            self._storages[slug] = obj
            path = join(self.ns, slug)
            self._ids[path] = obj
            self._meta[path] = RemoteStat(size=0, is_dir=True)
        if self._storages and INTERNAL not in self._storages:
            # sin pistas: el primero es el interno
            first = next(iter(self._storages))
            obj = self._storages.pop(first)
            self._storages = {INTERNAL: obj, **self._storages}
            self._ids = {join(self.ns, k): v for k, v in self._storages.items()}
            self._meta = {join(self.ns, k): RemoteStat(size=0, is_dir=True) for k in self._storages}

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                _com().call(dev.Close, timeout=30)
            except Exception:  # noqa: BLE001
                pass

    def is_present(self) -> bool:
        return any(d["pnp"] == self.pnp_id for d in list_devices())

    def reconnect(self) -> None:
        self.close()
        _com().call(self._connect_com)

    def waiting_message(self) -> str:
        return ("Se desconectó el celular. Volvé a enchufarlo, desbloquealo y elegí «Transferencia de archivos»: "
                "sigo donde quedé…")

    def is_connection_error(self, exc: BaseException) -> bool:
        if super().is_connection_error(exc):
            return True
        # Errores COM típicos de desconexión (dispositivo quitado / no listo / desconectado)
        hr = getattr(exc, "hresult", None) or (exc.args[0] if getattr(exc, "args", None) else None)
        return isinstance(hr, int) and (hr & 0xFFFFFFFF) in (0x8007048F, 0x80070015, 0x800704C9, 0x8007001F,
                                                              0x80042009, 0x802A0006)

    # ------------------------------------------------------------------ navegación
    def _ensure_children(self, path: str) -> list[str]:
        path = path.strip("/")
        if path in self._kids:
            return self._kids[path]
        if path == self.ns:
            self._kids[path] = sorted(self._storages)
            return self._kids[path]
        obj = self._resolve(path)
        if obj is None:
            raise FileNotFoundError(path)
        names = _com().call(self._list_com, path, obj)
        self._kids[path] = names
        return names

    def _list_com(self, path: str, obj: str) -> list[str]:
        w = _wpd()
        names = []
        for child in w.children(self._content, obj):
            p = w.props(self._props, child, [(FMT_OBJ, P_ORIGINAL_NAME), (FMT_OBJ, P_NAME), (FMT_OBJ, P_SIZE),
                                             (FMT_OBJ, P_CONTENT_TYPE), (FMT_OBJ, P_DATE_MODIFIED),
                                             (FMT_OBJ, P_DATE_CREATED)])
            name = str(p.get((FMT_OBJ, P_ORIGINAL_NAME)) or p.get((FMT_OBJ, P_NAME)) or "").strip()
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                continue
            ct = str(p.get((FMT_OBJ, P_CONTENT_TYPE)) or "").upper()
            is_dir = ct in (CT_FOLDER, CT_FUNCTIONAL)
            size = p.get((FMT_OBJ, P_SIZE))
            mtime = p.get((FMT_OBJ, P_DATE_MODIFIED)) or p.get((FMT_OBJ, P_DATE_CREATED))
            cpath = join(path, name)
            if cpath in self._ids:          # nombre repetido en la misma carpeta: se ignora el segundo
                continue
            self._ids[cpath] = child
            self._meta[cpath] = RemoteStat(size=int(size) if isinstance(size, int) else 0, is_dir=is_dir,
                                           mtime=mtime if isinstance(mtime, datetime) else None)
            names.append(name)
        return sorted(names)

    def _resolve(self, path: str) -> Optional[str]:
        path = path.strip("/")
        if not self.ns or (path != self.ns and not path.startswith(self.ns + "/")):
            raise PermissionError("ruta fuera del celular conectado")
        if path in self._ids:
            return self._ids[path]
        parent = path.rsplit("/", 1)[0]
        if parent == path or len(parent) < len(self.ns):
            return None
        self._ensure_children(parent)
        return self._ids.get(path)

    def listdir(self, path: str) -> list[str]:
        if self._dev is None:
            raise DisconnectedError("El celular no está conectado.")
        return list(self._ensure_children(path))

    def stat(self, path: str) -> Optional[RemoteStat]:
        path = path.strip("/")
        if path == self.ns:
            return RemoteStat(size=0, is_dir=True)
        try:
            if self._resolve(path) is None:
                return None
        except FileNotFoundError:
            return None
        return self._meta.get(path)

    def stat_many(self, paths: list[str]) -> dict[str, Optional[RemoteStat]]:
        return {p: self.stat(p) for p in paths}

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        obj = self._resolve(path)
        if obj is None:
            raise FileNotFoundError(path)
        w = _wpd()
        stream, block = _com().call(w.open_stream, self._transfer, obj)
        holder = [stream]
        del stream
        st = self._meta.get(path.strip("/"))
        expected = (st.size - offset) if st and st.size else None
        served = 0
        skip = offset
        try:
            while True:
                data = _com().call(w.read, holder[0], block, timeout=300)
                if not data:
                    break
                if skip:
                    # MTP no permite saltar: al retomar se lee y se descarta lo que ya está en la compu
                    cut = min(skip, len(data))
                    data, skip = data[cut:], skip - cut
                    if not data:
                        continue
                served += len(data)
                yield data
        finally:
            try:
                _com().call(holder.clear, timeout=30)   # se libera el stream en el hilo COM
            except Exception:  # noqa: BLE001
                pass
        if expected is not None and served < expected and not self.is_present():
            raise DisconnectedError("Se desconectó el celular.")
