"""Fuente iCloud Fotos (opcional), usando la librería de código abierto pyicloud.

Privacidad y seguridad:
  - Tu Apple ID y contraseña van DIRECTO a los servidores de Apple (idmsa.apple.com / icloud.com).
    No pasan por ningún servidor nuestro: no existe ningún servidor nuestro.
  - La contraseña NUNCA se guarda. Se guarda solo la sesión que entrega Apple (cookies) en tu
    carpeta de usuario, para no pedirte el código de 2 pasos cada vez. «Cerrar sesión» la borra.
  - Con iCloud activado, el control de red permite únicamente dominios de Apple.
  - Requisitos de Apple: «Acceso a los datos de iCloud en la web» activado y SIN
    «Protección de datos avanzada» (con ADP Apple no permite este tipo de acceso).
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote
from typing import Callable, Iterator, Optional

from ..config import config_dir
from ..models import AVAIL_LOCAL, ROLE_LIVE_VIDEO, ROLE_ORIGINAL, DeviceInfo, MediaItem, RemoteFile, RemoteStat
from ..security import no_network
from ..security.hotspot import MESSAGE as HOTSPOT_MESSAGE
from .base import CHUNK_SIZE, DeviceBackend, DeviceError, DisconnectedError

log = logging.getLogger(__name__)

EXPIRED_STATUS = {401, 403, 404, 410}
URL_MAX_AGE = 30 * 60      # los enlaces de descarga de iCloud vencen: se renuevan cada 30 minutos


def _status_of(exc: BaseException):
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) or getattr(exc, "code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def session_dir() -> Path:
    d = config_dir() / "icloud_session"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def forget_session() -> None:
    shutil.rmtree(config_dir() / "icloud_session", ignore_errors=True)


def _friendly(e: BaseException) -> str:
    name = type(e).__name__
    text = str(e)
    if "ServiceNotActivated" in name or "ADP" in text or "Advanced Data Protection" in text:
        return ("Apple no permite el acceso. Revisá en el iPhone: Ajustes > [tu nombre] > iCloud > activá "
                "«Acceso a los datos de iCloud en la web» y desactivá «Protección de datos avanzada».")
    if "AccountLocked" in name:
        return "Apple bloqueó la cuenta temporalmente. Entrá a iforgot.apple.com para desbloquearla."
    if "FailedLogin" in name or "Password" in name:
        return "Apple ID o contraseña incorrectos."
    if "SecurityKey" in name:
        return "Tu cuenta usa llaves de seguridad físicas; este método de inicio de sesión todavía no está soportado."
    if "NetworkBlocked" in name:
        return f"El control de red bloqueó una conexión que no era de Apple: {text}"
    if "Connection" in name or "Timeout" in name:
        return "No hay conexión con iCloud. Revisá internet e intentá de nuevo."
    return f"No se pudo iniciar sesión en iCloud: {text or name}"


class PhoneDataBlocked(DeviceError):
    """La compu está saliendo a internet por los datos móviles del iPhone: iCloud se frena."""


def _guard_phone_data() -> None:
    if no_network._phone_data_blocked():  # noqa: SLF001
        raise PhoneDataBlocked(HOTSPOT_MESSAGE)


class ICloudAccount:
    """Inicio de sesión con verificación en 2 pasos."""

    def __init__(self, apple_id: str):
        self.apple_id = apple_id.strip()
        self.api = None

    @property
    def is_demo(self) -> bool:
        return False

    def login(self, password: Optional[str]) -> str:
        """Devuelve "ok" o "2fa" (hay que pedir el código). Lanza DeviceError con mensaje claro."""
        no_network.allow_icloud(True)
        _guard_phone_data()
        try:
            from pyicloud import PyiCloudService
            self.api = PyiCloudService(self.apple_id, password, cookie_directory=str(session_dir()))
        except Exception as e:  # noqa: BLE001
            raise DeviceError(_friendly(e)) from e
        if self.api.requires_2fa:
            return "2fa"
        if getattr(self.api, "requires_2sa", False):
            raise DeviceError("Tu cuenta usa la verificación en 2 pasos antigua (2SA). Activá la autenticación de "
                              "doble factor en tu Apple ID para usar esta función.")
        return "ok"

    def resume(self) -> bool:
        """Intenta reusar la sesión guardada (sin contraseña)."""
        try:
            return self.login(None) == "ok"
        except DeviceError:
            return False

    def submit_code(self, code: str) -> bool:
        try:
            if not self.api.validate_2fa_code(code.strip()):
                return False
            if not self.api.is_trusted_session:
                self.api.trust_session()
            return True
        except Exception as e:  # noqa: BLE001
            raise DeviceError(_friendly(e)) from e


class ICloudBackend(DeviceBackend):
    source = "icloud"
    recover_timeout = 60 * 60

    def __init__(self, account: ICloudAccount):
        self.account = account
        self._assets: dict[str, object] = {}
        self._sizes: dict[str, int] = {}
        self._stale = False
        self._listed_at = 0.0
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._expired_hits: dict[str, int] = {}

    # ------------------------------------------------------------ conexión
    def connect(self, on_status=None) -> DeviceInfo:
        if self.account.api is None:
            raise DeviceError("Primero iniciá sesión en iCloud.")
        return DeviceInfo(name=f"iCloud de {self.account.apple_id}", model="iCloud Fotos", ios_version="—",
                          udid=self.account.apple_id, connection="Internet (solo Apple)")

    def close(self) -> None:
        pass

    def is_present(self) -> bool:
        if no_network._phone_data_blocked():  # noqa: SLF001
            return False
        try:
            socket.getaddrinfo("www.icloud.com", 443)
            return True
        except OSError:
            return False

    def reconnect(self) -> None:
        try:
            self.account.api.authenticate(force_refresh=True)
        except TypeError:
            self.account.api.authenticate()
        except Exception:  # noqa: BLE001
            pass
        self._stale = True
        self._ensure_fresh()   # renueva los enlaces de descarga vencidos

    def waiting_message(self) -> str:
        return "Se cortó la conexión con iCloud. Espero a que vuelva internet y sigo donde quedé…"

    def is_connection_error(self, exc: BaseException) -> bool:
        if super().is_connection_error(exc):
            return True
        try:
            import requests
            return isinstance(exc, (requests.ConnectionError, requests.Timeout,
                                    requests.exceptions.ChunkedEncodingError))
        except ImportError:
            return False

    # ------------------------------------------------------------ listado
    def list_items(self, progress: Optional[Callable[[str], None]] = None) -> list[MediaItem]:
        _guard_phone_data()
        items: list[MediaItem] = []
        assets: dict[str, object] = {}
        sizes: dict[str, int] = {}
        files_by_asset: dict[str, list] = {}
        for n, a in enumerate(self.account.api.photos.all, start=1):
            if progress and n % 200 == 0:
                progress(f"Listando iCloud… {n} elementos")
            try:
                item = self._to_item(a, sizes)
            except Exception as e:  # noqa: BLE001
                log.warning("Asset de iCloud ignorado: %s", e)
                continue
            if item:
                assets[a.id] = a
                files_by_asset[a.id] = item.files
                items.append(item)
        with self._lock:
            self._assets, self._sizes = assets, sizes
            self._files_by_asset = files_by_asset
            self._listed_at = time.monotonic()
            self._stale = False
        return items

    def _ensure_fresh(self) -> None:
        """Si los enlaces vencieron (o pasaron 30 min), vuelve a listar para tener enlaces nuevos."""
        if not self._stale and time.monotonic() - self._listed_at < URL_MAX_AGE:
            return
        with self._refresh_lock:
            if self._stale or time.monotonic() - self._listed_at >= URL_MAX_AGE:
                log.info("Renovando los enlaces de descarga de iCloud…")
                self.list_items()

    @staticmethod
    def _to_item(a, sizes: dict) -> Optional[MediaItem]:
        res = a.resources
        orig = res.get("original")
        if orig is None or not orig.url:
            return None
        filename = a.filename or orig.filename or a.id
        kind = "video" if a.item_type == "movie" else "photo"
        files = [RemoteFile(f"icloud/{a.id}/original", int(orig.size or a.size or 0), ROLE_ORIGINAL, filename)]
        sizes[files[0].path] = files[0].size
        live = res.get("original_video") if kind == "photo" else None
        if live is not None and live.url:
            stem = filename.rsplit(".", 1)[0]
            f = RemoteFile(f"icloud/{a.id}/original_video", int(live.size or 0), ROLE_LIVE_VIDEO,
                           live.filename if live.filename and "." in live.filename else f"{stem}.MOV")
            files.append(f)
            sizes[f.path] = f.size
        created = a.created.astimezone() if a.created else None
        return MediaItem(
            key=f"icloud:{a.id}", primary_path=f"icloud/{a.id}/original", kind=kind, files=files,
            availability=AVAIL_LOCAL, capture_date=created, uuid=a.id, expected_size=files[0].size,
            is_live=len(files) > 1, is_favorite=bool(getattr(a, "favorite", False)),
            original_filename=filename, display_name=filename, source="icloud", in_photos_db=True,
        )

    # ------------------------------------------------------------ archivos
    def listdir(self, path: str) -> list[str]:
        raise DeviceError("iCloud no tiene carpetas")

    def stat(self, path: str) -> Optional[RemoteStat]:
        size = self._sizes.get(path)
        return None if size is None else RemoteStat(size=size, is_dir=False)

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        _guard_phone_data()
        self._ensure_fresh()
        _, asset_id, version = path.split("/", 2)
        asset = self._assets.get(asset_id)
        if asset is None:
            raise FileNotFoundError(path)
        url = asset.download_url(version)
        if not url:
            raise FileNotFoundError(path)
        # CloudKit deja «${f}» en el enlace para que el cliente ponga el nombre del archivo (como iCloud.com)
        name = next((f.name for f in self._files_of(asset_id) if f.path == path), asset_id)
        url = url.replace("${f}", quote(name))
        session = self.account.api.photos.session
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            resp = session.get(url, stream=True, headers=headers, timeout=(20, 120))
        except Exception as e:  # noqa: BLE001
            code = _status_of(e)
            if code in EXPIRED_STATUS or "no longer serves" in str(e):
                self._expired(path, e)
            if code == 429:
                time.sleep(30)
                raise DisconnectedError("iCloud pidió bajar el ritmo; espero y sigo.") from e
            raise
        if resp.status_code in EXPIRED_STATUS:
            resp.close()
            self._expired(path)
        if resp.status_code == 429:
            resp.close()
            time.sleep(30)
            raise DisconnectedError("iCloud pidió bajar el ritmo; espero y sigo.")
        resp.raise_for_status()
        self._expired_hits.pop(path, None)
        skip = offset if (offset and resp.status_code == 200) else 0   # el servidor ignoró el Range
        try:
            for chunk in resp.iter_content(chunk_size=min(chunk_size, 1024 * 1024)):
                if not chunk:
                    continue
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk, skip = chunk[skip:], 0
                yield chunk
        finally:
            resp.close()

    def _expired(self, path: str, cause=None):
        """Enlace vencido: se renueva y se reintenta. Si vuelve a fallar con enlace nuevo, es error real."""
        n = self._expired_hits.get(path, 0) + 1
        self._expired_hits[path] = n
        if n > 2:
            raise RuntimeError("iCloud rechaza la descarga incluso con enlaces nuevos (HTTP 410). Puede que Apple "
                               "haya cambiado algo: actualizá la app o probá más tarde.") from cause
        self._stale = True
        raise DisconnectedError("El enlace de descarga de iCloud venció; lo renuevo y sigo.") from cause

    def _files_of(self, asset_id: str) -> list:
        return getattr(self, "_files_by_asset", {}).get(asset_id, [])

    # ------------------------------------------------------------ borrar
    def delete_asset(self, item: MediaItem) -> bool:
        """Manda el elemento a «Eliminado recientemente» de iCloud (recuperable 30 días)."""
        asset = self._assets.get(item.uuid or "")
        if asset is None:
            raise DeviceError(f"No encuentro {item.name} en iCloud (volvé a listar).")
        return bool(asset.delete())
