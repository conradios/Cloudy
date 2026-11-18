"""Escaneo del iPhone: arma la lista de fotos/videos y detecta cuáles no tienen el original.

Pasos:
 1. Recorre DCIM/ (donde iOS guarda los originales) y agrupa archivos por nombre base
    (IMG_1234.HEIC + IMG_1234.MOV = Live Photo; IMG_1234.AAE = ajustes de edición).
 2. Busca versiones editadas en PhotoData/Mutations/... (FullSizeRender).
 3. Lee la base de Fotos para saber fecha, álbumes, favoritos y — lo más importante —
    qué assets existen en la fototeca pero cuyo original NO está en el iPhone (iCloud optimizado).
"""
from __future__ import annotations

import logging
import re
import shutil
from typing import Callable, Optional

from ..device.base import DeviceBackend
from ..models import (AVAIL_CLOUD_ONLY, AVAIL_LOCAL, AVAIL_OPTIMIZED, AVAIL_UNKNOWN, PHOTO_EXT, ROLE_AAE,
                      ROLE_EDITED, ROLE_LIVE_VIDEO, ROLE_ORIGINAL, VIDEO_EXT, MediaItem, RemoteFile, ScanResult)
from ..security.cleanup_tmp import new_tmpdir
from .photos_db import KIND_VIDEO, SUBTYPE_LIVE, SUBTYPE_SCREENSHOT, PhotosLibrary, fetch_photos_db

log = logging.getLogger(__name__)

DCIM = "DCIM"
MUTATIONS = "PhotoData/Mutations"
OPTIMIZED_RATIO = 0.90   # si el archivo pesa menos del 90 % del original esperado → versión optimizada

_EDITED_STEM = re.compile(r"^IMG_E(\d+)$", re.I)
_ORIG_AAE_STEM = re.compile(r"^IMG_O(\d+)$", re.I)

ProgressCb = Callable[[str, int, int], None]


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def _stem(name: str) -> str:
    i = name.rfind(".")
    return name[:i] if i >= 0 else name


def _list(backend: DeviceBackend, path: str) -> list[str]:
    try:
        return [n for n in backend.listdir(path) if not n.startswith(".")]
    except Exception:  # noqa: BLE001
        return []


class MediaScanner:
    def __init__(self, backend: DeviceBackend, progress: Optional[ProgressCb] = None):
        self.backend = backend
        self.progress = progress or (lambda msg, done, total: None)

    def scan(self) -> ScanResult:
        warnings: list[str] = []

        # ---------------------------------------------------------------- 1. DCIM
        self.progress("Leyendo carpetas del iPhone…", 0, 0)
        folders = [f for f in _list(self.backend, DCIM)]
        paths: list[str] = []
        for folder in folders:
            for name in _list(self.backend, f"{DCIM}/{folder}"):
                paths.append(f"{DCIM}/{folder}/{name}")
        self.progress(f"Analizando {len(paths)} archivos…", 0, len(paths))
        stats = {p: s for p, s in self.backend.stat_many(paths).items() if s is not None and not s.is_dir}

        # Agrupar por carpeta + nombre base
        groups: dict[tuple[str, str], list[tuple[str, str]]] = {}   # (folder, base) -> [(path, role_hint)]
        for path in stats:
            folder, name = path.rsplit("/", 1)
            stem = _stem(name)
            role_hint = ""
            m = _EDITED_STEM.match(stem)
            if m:
                stem, role_hint = f"IMG_{m.group(1)}", ROLE_EDITED
            m = _ORIG_AAE_STEM.match(stem)
            if m and _ext(name) == ".aae":
                stem, role_hint = f"IMG_{m.group(1)}", ROLE_AAE
            groups.setdefault((folder, stem.upper()), []).append((path, role_hint))

        items: dict[str, MediaItem] = {}
        for (folder, _), entries in groups.items():
            item = self._build_item(entries, stats)
            if item:
                items[item.primary_path.upper()] = item

        # ---------------------------------------------------------- 2. editadas
        self.progress("Buscando versiones editadas…", 0, 0)
        by_folder_stem = {(it.primary_path.rsplit("/", 1)[0], _stem(it.name).upper()): it for it in items.values()}
        for folder in folders:
            mut_dir = f"{MUTATIONS}/{DCIM}/{folder}"
            for stem_dir in _list(self.backend, mut_dir):
                adj_dir = f"{mut_dir}/{stem_dir}/Adjustments"
                renders = [n for n in _list(self.backend, adj_dir) if n.lower().startswith("fullsizerender")]
                if not renders:
                    continue
                owner = by_folder_stem.get((f"{DCIM}/{folder}", stem_dir.upper()))
                if owner is None:
                    continue
                for r in renders:
                    st = self.backend.stat(f"{adj_dir}/{r}")
                    if st and not st.is_dir:
                        owner.files.append(RemoteFile(f"{adj_dir}/{r}", st.size, ROLE_EDITED))
                        owner.has_edits = True

        # ------------------------------------------------------ 3. base de Fotos
        self.progress("Leyendo la base de datos de Fotos…", 0, 0)
        tmp = new_tmpdir("photosdb_")
        library: Optional[PhotosLibrary] = None
        try:
            db = fetch_photos_db(self.backend, tmp)
            if db:
                try:
                    library = PhotosLibrary.load(db)
                except Exception as e:  # noqa: BLE001
                    log.warning("No pude interpretar Photos.sqlite: %s", e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        scan_stats = {"dcim_files": len(stats), "dcim_folders": len(folders), "items_in_dcim": len(items)}

        if library is None:
            for it in items.values():
                it.availability = AVAIL_UNKNOWN
            warnings.append(
                "No pude leer la base de datos de Fotos del iPhone, así que NO puedo detectar fotos optimizadas "
                "(las que tienen el original solo en iCloud). Se copia todo lo que está en el iPhone, pero revisá "
                "en el iPhone: Ajustes > [tu nombre] > iCloud > Fotos > «Descargar y conservar originales»."
            )
        else:
            self._apply_library(items, library)
            scan_stats.update(library.stats())

        result_items = list(items.values())
        cloud = [i for i in result_items if i.availability == AVAIL_CLOUD_ONLY and not i.is_trashed]
        optimized = [i for i in result_items if i.availability == AVAIL_OPTIMIZED and not i.is_trashed]
        scan_stats.update({
            "items_total": len(result_items),
            "cloud_only": len(cloud),
            "optimized": len(optimized),
            "local": sum(1 for i in result_items if i.is_available and not i.is_trashed),
            "trashed_items": sum(1 for i in result_items if i.is_trashed),
            "not_in_photos_db": sum(1 for i in result_items if not i.in_photos_db),
        })
        if cloud or optimized:
            warnings.append(
                f"{len(cloud) + len(optimized)} fotos/videos NO tienen el original en alta calidad en el iPhone "
                f"({len(cloud)} solo en iCloud, {len(optimized)} en versión optimizada). No se copian: quedan en la "
                "lista de «Pendientes». Para bajarlos: en el iPhone, Ajustes > [tu nombre] > iCloud > Fotos > "
                "«Descargar y conservar originales» y después «Buscar en el iPhone» (en Pendientes), o conectá iCloud "
                "para bajarlos directo en calidad original."
            )
        self.progress("Escaneo terminado", 1, 1)
        return ScanResult(items=result_items, photos_db_available=library is not None,
                          warnings=warnings, stats=scan_stats)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _build_item(entries: list[tuple[str, str]], stats) -> Optional[MediaItem]:
        photos = [p for p, h in entries if not h and _ext(p) in PHOTO_EXT]
        videos = [p for p, h in entries if not h and _ext(p) in VIDEO_EXT]
        if photos:
            primary, kind = sorted(photos)[0], "photo"
        elif videos:
            primary, kind = sorted(videos)[0], "video"
        else:
            return None
        item = MediaItem(key=primary, primary_path=primary, kind=kind, availability=AVAIL_LOCAL)
        st = stats[primary]
        d = st.birthtime or st.mtime
        item.capture_date = d.astimezone() if d is not None else None   # siempre con zona horaria
        item.files.append(RemoteFile(primary, st.size, ROLE_ORIGINAL))
        for path, hint in entries:
            if path == primary:
                continue
            ext = _ext(path)
            if hint == ROLE_EDITED:
                role = ROLE_EDITED
                item.has_edits = True
            elif ext == ".aae" or hint == ROLE_AAE:
                role = ROLE_AAE
            elif kind == "photo" and ext in VIDEO_EXT:
                role = ROLE_LIVE_VIDEO
                item.is_live = True
            elif ext in PHOTO_EXT or ext in VIDEO_EXT:
                role = ROLE_ORIGINAL   # p. ej. RAW+JPG del mismo disparo
            else:
                continue
            item.files.append(RemoteFile(path, stats[path].size, role))
        return item

    @staticmethod
    def _apply_library(items: dict[str, MediaItem], library: PhotosLibrary) -> None:
        for asset in library.assets:
            if asset.is_shared_stream:
                continue   # álbumes compartidos: no son parte de tu fototeca
            item = items.get(asset.path.upper())
            if item is None:
                # Existe en Fotos pero el archivo original no está en el iPhone → solo en iCloud
                item = MediaItem(
                    key=asset.uuid, primary_path=asset.path,
                    kind="video" if asset.kind == KIND_VIDEO else "photo",
                    availability=AVAIL_CLOUD_ONLY,
                )
                items[asset.path.upper()] = item
            else:
                orig = item.files_by_role(ROLE_ORIGINAL)[0]
                if asset.original_size and orig.size < asset.original_size * OPTIMIZED_RATIO:
                    item.availability = AVAIL_OPTIMIZED
                else:
                    item.availability = AVAIL_LOCAL
            item.key = asset.uuid
            item.uuid = asset.uuid
            item.in_photos_db = True
            item.expected_size = asset.original_size
            item.capture_date = asset.created or item.capture_date
            item.is_live = item.is_live or asset.subtype == SUBTYPE_LIVE
            item.is_screenshot = asset.subtype == SUBTYPE_SCREENSHOT
            item.is_hidden = asset.hidden
            item.is_favorite = asset.favorite
            item.is_trashed = asset.trashed
            item.has_edits = item.has_edits or asset.has_adjustments
            item.albums = sorted(set(asset.albums))
            item.original_filename = asset.original_filename
