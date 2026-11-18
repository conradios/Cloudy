"""Lectura de la base de datos de la app Fotos del iPhone (PhotoData/Photos.sqlite).

Se copia la base (y sus archivos -wal / -shm) a una carpeta temporal de la compu y se lee ahí.
Nunca se modifica nada en el iPhone. Con esta base sabemos, para cada foto/video:
  - si el original realmente está en el iPhone o solo en iCloud ("Optimizar almacenamiento"),
  - el tamaño original esperado, la fecha de captura, favorito, oculto, álbumes, Live Photo, etc.

El esquema cambia entre versiones de iOS, así que todo se consulta de forma defensiva.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..device.base import DeviceBackend

log = logging.getLogger(__name__)

DB_REMOTE = "PhotoData/Photos.sqlite"
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

KIND_PHOTO = 0
KIND_VIDEO = 1
SUBTYPE_LIVE = 2
SUBTYPE_SCREENSHOT = 10
SAVED_TYPE_SHARED_STREAM = 2


@dataclass
class PhotoAsset:
    pk: int
    uuid: str
    directory: str
    filename: str
    kind: int
    subtype: int = 0
    created: Optional[datetime] = None
    trashed: bool = False
    hidden: bool = False
    favorite: bool = False
    has_adjustments: bool = False
    saved_type: Optional[int] = None
    original_size: Optional[int] = None
    original_filename: Optional[str] = None
    albums: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        d = self.directory.strip("/")
        return f"{d}/{self.filename}" if d else self.filename

    @property
    def is_shared_stream(self) -> bool:
        return (not self.directory.upper().startswith("DCIM")) and (
            self.saved_type == SAVED_TYPE_SHARED_STREAM or "PHOTOSTREAM" in self.directory.upper()
        )


def core_data_to_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return (CORE_DATA_EPOCH + timedelta(seconds=float(value))).astimezone()
    except (OverflowError, ValueError, TypeError):
        return None


def fetch_photos_db(backend: DeviceBackend, workdir: Optional[Path] = None) -> Optional[Path]:
    """Copia Photos.sqlite (+ -wal, -shm) del iPhone a una carpeta temporal local."""
    workdir = Path(workdir or tempfile.mkdtemp(prefix="iphone_vault_db_"))
    workdir.mkdir(parents=True, exist_ok=True)
    local_db = workdir / "Photos.sqlite"
    try:
        if not backend.exists(DB_REMOTE):
            log.warning("No existe %s en el iPhone", DB_REMOTE)
            return None
        for suffix in ("", "-wal", "-shm"):
            remote = DB_REMOTE + suffix
            if suffix and not backend.exists(remote):
                continue
            with open(str(local_db) + suffix, "wb") as fh:
                for chunk in backend.read_chunks(remote):
                    fh.write(chunk)
        return local_db
    except Exception as e:  # noqa: BLE001
        log.warning("No pude copiar la base de Fotos: %s", e)
        return None


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not _SAFE_NAME.match(table):
        return set()
    return {str(row[1]).upper() for row in con.execute(f'PRAGMA table_info("{table}")')
            if _SAFE_NAME.match(str(row[1]))}


def _tables(con: sqlite3.Connection) -> set[str]:
    """Solo nombres de tabla «normales»: la base viene del teléfono y no confiamos en ella."""
    return {str(r[0]).upper() for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if _SAFE_NAME.match(str(r[0]))}


class PhotosLibrary:
    def __init__(self, assets: list[PhotoAsset], schema_info: dict):
        self.assets = assets
        self.schema_info = schema_info
        self.by_path: dict[str, PhotoAsset] = {a.path.upper(): a for a in assets}

    @classmethod
    def load(cls, db_path: Path) -> "PhotosLibrary":
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA trusted_schema=OFF")   # no ejecutar funciones/vistas definidas en la base
        try:
            return cls._load(con)
        finally:
            con.close()

    @classmethod
    def _load(cls, con: sqlite3.Connection) -> "PhotosLibrary":
        tables = _tables(con)
        asset_table = "ZASSET" if "ZASSET" in tables else ("ZGENERICASSET" if "ZGENERICASSET" in tables else None)
        if asset_table is None:
            raise ValueError("La base de Fotos no tiene la tabla de assets esperada (ZASSET/ZGENERICASSET).")
        cols = _columns(con, asset_table)

        wanted = ["Z_PK", "ZUUID", "ZDIRECTORY", "ZFILENAME", "ZKIND", "ZKINDSUBTYPE", "ZDATECREATED",
                  "ZTRASHEDSTATE", "ZHIDDEN", "ZFAVORITE", "ZHASADJUSTMENTS", "ZSAVEDASSETTYPE"]
        select = ", ".join(f"a.{c}" if c in cols else f"NULL AS {c}" for c in wanted)

        join = ""
        extra = "NULL AS ZORIGINALFILESIZE, NULL AS ZORIGINALFILENAME"
        if "ZADDITIONALASSETATTRIBUTES" in tables:
            acols = _columns(con, "ZADDITIONALASSETATTRIBUTES")
            if "ZASSET" in acols:
                join = "LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK"
                extra = ", ".join(
                    f"aa.{c}" if c in acols else f"NULL AS {c}" for c in ("ZORIGINALFILESIZE", "ZORIGINALFILENAME")
                )

        # nombres de tabla/columna leídos del propio esquema y validados con _SAFE (solo [A-Z0-9_])
        sql = f"SELECT {select}, {extra} FROM {asset_table} a {join}"  # nosec B608
        assets: list[PhotoAsset] = []
        by_pk: dict[int, PhotoAsset] = {}
        skipped = 0
        for row in con.execute(sql):
            try:
                (pk, uuid, directory, filename, kind, subtype, created, trashed, hidden, fav, adj, saved,
                 osize, oname) = row
                if not filename or not isinstance(filename, str):
                    continue
                a = PhotoAsset(
                    pk=pk, uuid=str(uuid or f"pk-{pk}"), directory=str(directory or ""), filename=filename,
                    kind=int(kind or 0), subtype=int(subtype or 0), created=core_data_to_datetime(created),
                    trashed=bool(trashed), hidden=bool(hidden), favorite=bool(fav), has_adjustments=bool(adj),
                    saved_type=saved, original_size=int(osize) if osize else None,
                    original_filename=oname if isinstance(oname, str) else None,
                )
            except (TypeError, ValueError, OverflowError):
                skipped += 1      # fila rara: se ignora esa fila, no toda la base
                continue
            assets.append(a)
            by_pk[pk] = a

        albums_found = cls._load_albums(con, tables, by_pk)
        info = {"asset_table": asset_table, "asset_columns": sorted(cols), "albums_linked": albums_found,
                "rows_skipped": skipped}
        return cls(assets, info)

    @staticmethod
    def _load_albums(con, tables: set[str], by_pk: dict[int, PhotoAsset]) -> bool:
        """Álbumes de usuario. La tabla intermedia se llama distinto según iOS (Z_26ASSETS, Z_28ASSETS...)."""
        if "ZGENERICALBUM" not in tables:
            return False
        try:
            album_cols = _columns(con, "ZGENERICALBUM")
            kind_filter = "WHERE ZKIND = 2" if "ZKIND" in album_cols else ""
            q = f"SELECT Z_PK, ZTITLE FROM ZGENERICALBUM {kind_filter}"  # nosec B608 - filtro fijo
            titles = {pk: t for pk, t in con.execute(q) if t}
            for t in tables:
                if not re.fullmatch(r"Z_\d+ASSETS", t):
                    continue
                cols = _columns(con, t)
                album_col = next((c for c in cols if re.fullmatch(r"Z_\d+ALBUMS", c)), None)
                asset_col = next((c for c in cols if re.fullmatch(r"Z_\d+ASSETS", c)), None)
                if not album_col or not asset_col:
                    continue
                for album_pk, asset_pk in con.execute(f"SELECT {album_col}, {asset_col} FROM {t}"):  # nosec B608 (regex)
                    if album_pk in titles and asset_pk in by_pk:
                        by_pk[asset_pk].albums.append(titles[album_pk])
                return True
        except sqlite3.Error as e:
            log.info("No pude leer álbumes: %s", e)
        return False

    def stats(self) -> dict:
        prefixes: dict[str, int] = {}
        saved: dict[str, int] = {}
        for a in self.assets:
            p = a.directory.split("/")[0] or "(vacío)"
            prefixes[p] = prefixes.get(p, 0) + 1
            saved[str(a.saved_type)] = saved.get(str(a.saved_type), 0) + 1
        return {
            "assets_total": len(self.assets),
            "trashed": sum(a.trashed for a in self.assets),
            "hidden": sum(a.hidden for a in self.assets),
            "shared_streams": sum(a.is_shared_stream for a in self.assets),
            "by_directory_prefix": prefixes,
            "by_saved_asset_type": saved,
        }
