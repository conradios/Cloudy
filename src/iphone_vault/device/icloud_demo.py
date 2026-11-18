"""iCloud SIMULADO para probar sin cuenta ni internet.

Contiene la misma fototeca que el iPhone simulado (con los originales en alta calidad, incluidos
los que en el iPhone están «solo en iCloud») y 2 elementos que solo existen en iCloud.
Inicio de sesión de prueba: cualquier contraseña; código de 2 pasos: 123456.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..models import AVAIL_LOCAL, ROLE_LIVE_VIDEO, ROLE_ORIGINAL, DeviceInfo, MediaItem, RemoteFile, RemoteStat
from .base import CHUNK_SIZE, DeviceBackend, DeviceError
from .demo import DEMO_ASSETS, _blob, _full_content, demo_root

DEMO_CODE = "123456"
EXTRA = [
    ("IMG_0018.HEIC", 0, "2026-10-20 09:30", {}),
    ("IMG_0019.MOV", 1, "2026-10-28 18:00", {}),
]


class DemoICloudAccount:
    def __init__(self, apple_id: str):
        self.apple_id = apple_id.strip() or "demo@icloud.com"
        self.api = None

    @property
    def is_demo(self) -> bool:
        return True

    def login(self, password: Optional[str]) -> str:
        if not password:
            raise DeviceError("Ingresá cualquier contraseña (modo demo).")
        self.api = "pending"
        return "2fa"

    def resume(self) -> bool:
        return False

    def submit_code(self, code: str) -> bool:
        if code.strip() != DEMO_CODE:
            return False
        self.api = "ok"
        return True


class DemoICloudBackend(DeviceBackend):
    source = "icloud"
    recover_timeout = 30

    def __init__(self, account: DemoICloudAccount, root: Optional[Path] = None):
        self.account = account
        self.root = Path(root or demo_root()) / "icloud"
        self.root.mkdir(parents=True, exist_ok=True)
        self._deleted_file = self.root / "deleted.json"
        self._items: dict[str, MediaItem] = {}
        self._paths: dict[str, Path] = {}

    @property
    def is_demo(self) -> bool:
        return True

    def connect(self, on_status=None) -> DeviceInfo:
        return DeviceInfo(name=f"iCloud de {self.account.apple_id} (DEMO)", model="iCloud Fotos simulado",
                          ios_version="—", udid=self.account.apple_id, connection="Simulado")

    def close(self) -> None:
        pass

    def _deleted(self) -> set[str]:
        try:
            return set(json.loads(self._deleted_file.read_text()))
        except (OSError, ValueError):
            return set()

    def _materialize(self, name: str, kind: int, seed: int) -> Path:
        p = self.root / f"{seed:04d}_{name}"
        if not p.exists():
            p.write_bytes(_full_content(name, kind, seed))
        return p

    def list_items(self, progress: Optional[Callable[[str], None]] = None) -> list[MediaItem]:
        deleted = self._deleted()
        entries = [(pk, name, kind, sub, when, flags)
                   for pk, (_f, name, kind, sub, when, flags) in enumerate(DEMO_ASSETS, start=1)]
        entries += [(100 + i, name, kind, 0, when, flags) for i, (name, kind, when, flags) in enumerate(EXTRA)]
        items = []
        for pk, name, kind, sub, when, flags in entries:
            if flags.get("trashed"):
                continue
            aid = f"DEMOCLOUD{pk:04d}"
            if aid in deleted:
                continue
            orig = self._materialize(name, kind, pk)
            path = f"icloud/{aid}/original"
            self._paths[path] = orig
            files = [RemoteFile(path, orig.stat().st_size, ROLE_ORIGINAL, name)]
            if flags.get("live"):
                vp = self.root / f"{pk:04d}_{name.rsplit('.', 1)[0]}.MOV"
                if not vp.exists():
                    vp.write_bytes(_blob(2_500_000, pk + 1000))
                vpath = f"icloud/{aid}/original_video"
                self._paths[vpath] = vp
                files.append(RemoteFile(vpath, vp.stat().st_size, ROLE_LIVE_VIDEO, vp.name.split("_", 1)[1]))
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M").astimezone()
            it = MediaItem(key=f"icloud:{aid}", primary_path=path, kind="video" if kind == 1 else "photo",
                           files=files, availability=AVAIL_LOCAL, capture_date=dt, uuid=aid,
                           expected_size=files[0].size, is_live=len(files) > 1, is_favorite=bool(flags.get("fav")),
                           is_screenshot=sub == 10, original_filename=name, display_name=name,
                           source="icloud", in_photos_db=True)
            self._items[aid] = it
            items.append(it)
        return items

    def listdir(self, path: str) -> list[str]:
        raise DeviceError("iCloud no tiene carpetas")

    def stat(self, path: str) -> Optional[RemoteStat]:
        p = self._paths.get(path)
        return RemoteStat(size=p.stat().st_size, is_dir=False) if p else None

    def read_chunks(self, path: str, chunk_size: int = CHUNK_SIZE, offset: int = 0) -> Iterator[bytes]:
        p = self._paths.get(path)
        if p is None:
            raise FileNotFoundError(path)
        with open(p, "rb") as fh:
            fh.seek(offset)
            while True:
                data = fh.read(min(chunk_size, 512 * 1024))
                if not data:
                    break
                yield data

    def delete_asset(self, item: MediaItem) -> bool:
        deleted = self._deleted()
        deleted.add(item.uuid)
        self._deleted_file.write_text(json.dumps(sorted(deleted)))
        return True
