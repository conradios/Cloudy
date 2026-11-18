"""Selección y copia verificada de archivos (desde el iPhone por cable o desde iCloud).

Cada archivo se copia así:
  1. Se lee por bloques y se escribe en "<nombre>.part" calculando SHA-256 al vuelo.
     Si ya existía un .part de un intento anterior (corte de cable, reinicio, enlace vencido),
     se RETOMA desde donde quedó en vez de empezar de cero.
  2. flush + fsync (que quede físicamente en el disco).
  3. Tamaño exacto: tiene que coincidir byte a byte con el original.
  4. SHA-256: se relee el .part desde el disco y se compara con el calculado al leer.
  5. Recién ahí se renombra al nombre final (operación atómica).
  6. Estructura: se valida el contenedor (HEIC/MOV/JPEG/PNG…). Como la copia ya es idéntica
     al original, una estructura rara se informa como advertencia, no se descarta la copia.
  7. Si hay destinos espejo (NAS, disco externo, carpeta de OneDrive…), se replica y se verifica.

Si se corta la conexión, el motor espera a que vuelva y sigue exactamente donde estaba.
Nunca se borra ni modifica nada en el origen.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from ..catalog.manifest import STATUS_DONE, STATUS_EXCLUDED, STATUS_PENDING, Manifest
from ..config import TransferOptions
from ..device.base import CHUNK_SIZE, DeviceBackend, DeviceError
from ..models import (AVAIL_CLOUD_ONLY, AVAIL_OPTIMIZED, ROLE_AAE, ROLE_EDITED, ROLE_LIVE_VIDEO, MediaItem,
                      RemoteFile)
from ..organizer.filetimes import set_file_times
from ..organizer.layout import local_relpath, with_suffix_number
from .validate import validate_file

log = logging.getLogger(__name__)

MAX_RETRIES = 2
PREFETCH = 4          # bloques leídos por adelantado (4 × 4 MB)
REASONS = {
    AVAIL_CLOUD_ONLY: "Original solo en iCloud (Optimizar almacenamiento)",
    AVAIL_OPTIMIZED: "En el iPhone hay solo una versión optimizada (menor calidad)",
}
ROLE_LABEL = {"original": "original", ROLE_LIVE_VIDEO: "video Live Photo", ROLE_EDITED: "editada", ROLE_AAE: "ajustes"}

STATE_COPYING = "copiando"
STATE_PAUSED = "pausado"
STATE_WAITING = "esperando conexión"
STATE_CONVERTING = "creando copias compatibles"


class Cancelled(Exception):
    pass


class VerificationError(Exception):
    pass


class SourceError(Exception):
    """Error al leer del origen (iPhone/iCloud), para distinguirlo de errores del disco local."""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class SourceUnavailable(DeviceError):
    pass


class DestinationFull(SourceUnavailable):
    pass


def _is_disk_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and (e.errno == 28 or getattr(e, "winerror", None) in (39, 112))


class UnsafePathError(Exception):
    """La ruta final saldría de la carpeta destino (p. ej. por un enlace simbólico)."""


def ensure_inside(root: Path, target: Path) -> None:
    root_real = root.resolve()
    real = target.resolve()
    if real != root_real and root_real not in real.parents:
        raise UnsafePathError(f"la ruta {target} sale de la carpeta destino (¿enlace simbólico?): no se escribe")


@dataclass
class Plan:
    entries: list[tuple[MediaItem, list[RemoteFile]]]
    already_done: int = 0
    excluded_by_filters: int = 0
    unavailable: int = 0

    @property
    def items(self) -> list[MediaItem]:
        return [it for it, _ in self.entries]

    @property
    def file_count(self) -> int:
        return sum(len(fs) for _, fs in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for _, fs in self.entries for f in fs)


@dataclass
class TransferReport:
    copied_files: int = 0
    copied_bytes: int = 0
    resumed_files: int = 0
    skipped_files: int = 0
    items_done: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reconnections: int = 0
    device_verified: int = 0          # archivos cuyo SHA-256 también se comprobó en el propio celular
    compat_created: int = 0
    compat_failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    interrupted: Optional[str] = None
    seconds: float = 0.0


@dataclass
class Progress:
    file_index: int
    file_count: int
    bytes_done: int
    bytes_total: int
    current: str
    speed_bps: float
    eta_seconds: Optional[float] = None
    items_done: int = 0
    items_total: int = 0
    errors: int = 0
    warnings: int = 0
    state: str = STATE_COPYING
    message: str = ""


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def register_unavailable(items: list[MediaItem], manifest: Manifest) -> int:
    """Pone en la lista de pendientes todo lo que no tiene el original disponible."""
    done_fps = manifest.done_fingerprints()
    n = 0
    for it in items:
        if it.availability in REASONS and not it.is_trashed:
            manifest.mark_pending(it, REASONS[it.availability], done_fps)
            n += 1
    return n


def wanted_files(item: MediaItem, opt: TransferOptions) -> list[RemoteFile]:
    out = []
    for f in item.files:
        if f.role == ROLE_LIVE_VIDEO and not opt.include_live_videos:
            continue
        if f.role == ROLE_EDITED and not opt.include_edited:
            continue
        if f.role == ROLE_AAE and not opt.include_aae:
            continue
        out.append(f)
    return out


def passes_filters(item: MediaItem, opt: TransferOptions) -> bool:
    if item.is_trashed:
        return False
    if item.kind == "photo" and not opt.include_photos:
        return False
    if item.kind == "video" and not opt.include_videos:
        return False
    if item.is_screenshot and not opt.include_screenshots:
        return False
    if item.is_hidden and not opt.include_hidden:
        return False
    if opt.only_favorites and not item.is_favorite:
        return False
    d_from, d_to = _parse_date(opt.date_from), _parse_date(opt.date_to)
    if (d_from or d_to) and item.capture_date:
        d = item.capture_date.date()
        if d_from and d < d_from:
            return False
        if d_to and d > d_to:
            return False
    return True


def build_plan(items: list[MediaItem], opt: TransferOptions, manifest: Manifest,
               only_pending: bool = False) -> Plan:
    pending_keys = manifest.keys_with_status(STATUS_PENDING) if only_pending else None
    pending_fps = manifest.pending_fingerprints() if only_pending else None
    excluded_keys = manifest.keys_with_status(STATUS_EXCLUDED)
    done_fps = manifest.done_fingerprints()
    own_done = manifest.keys_with_status(STATUS_DONE)
    candidates, already, excluded, unavailable = [], 0, 0, 0
    seen_fps: set[str] = set()
    for it in items:
        if pending_keys is not None and it.key not in pending_keys and it.fingerprint not in pending_fps:
            continue
        if it.key in excluded_keys or not passes_filters(it, opt):
            excluded += 1
            continue
        if not it.is_available:
            unavailable += 1
            continue
        todo = [f for f in wanted_files(it, opt) if not manifest.is_file_done(f.path, f.size)]
        fp = it.fingerprint
        if not todo or (fp and fp in done_fps and it.key not in own_done):
            already += 1   # ya copiado (por esta u otra fuente)
            continue
        if fp:
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
        candidates.append((it, todo))

    candidates.sort(key=lambda c: c[0].capture_date.timestamp() if c[0].capture_date else 0.0,
                    reverse=opt.newest_first)
    if opt.limit and opt.limit > 0:
        candidates = candidates[: opt.limit]
    return Plan(entries=candidates, already_done=already, excluded_by_filters=excluded,
                unavailable=unavailable)


class _Speed:
    """Velocidad promedio de los últimos segundos (para que el tiempo restante no salte)."""

    def __init__(self, window: float = 15.0):
        self.window = window
        self.samples: deque = deque()

    def add(self, t: float, total_bytes: int):
        self.samples.append((t, total_bytes))
        while self.samples and t - self.samples[0][0] > self.window:
            self.samples.popleft()

    def bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def reset(self):
        self.samples.clear()


class TransferEngine:
    def __init__(self, backend: DeviceBackend, manifest: Manifest, options: TransferOptions,
                 progress: Optional[Callable[[Progress], None]] = None,
                 log_cb: Optional[Callable[[str], None]] = None,
                 cancel_event: Optional[threading.Event] = None,
                 pause_event: Optional[threading.Event] = None):
        self.backend = backend
        self.manifest = manifest
        self.opt = options
        self.root = manifest.root
        self.progress_cb = progress or (lambda p: None)
        self.log = log_cb or (lambda m: log.info(m))
        self.cancel = cancel_event or threading.Event()
        self.pause = pause_event or threading.Event()
        self._speed = _Speed()
        self._state = STATE_COPYING
        self._message = ""
        self.report = TransferReport()

    # ----------------------------------------------------------------- público
    def run(self, plan: Plan) -> TransferReport:
        """Copia el plan. La compu no se suspende mientras dura; al final se respalda el catálogo."""
        from ..system.power import KeepAwake
        with KeepAwake():
            try:
                return self._run(plan)
            finally:
                try:
                    self.manifest.backup()
                except Exception as e:  # noqa: BLE001
                    self.log(f"AVISO: no se pudo respaldar el catálogo: {e}")

    def _run(self, plan: Plan) -> TransferReport:
        from ..system.disk import FAT_LIMIT, disk_info
        rep = self.report = TransferReport()
        start = time.monotonic()
        free = shutil.disk_usage(self.root).free
        if plan.total_bytes > free:
            raise OSError(f"No hay espacio suficiente en el destino: se necesitan {plan.total_bytes} bytes "
                          f"y hay {free} libres.")
        di = disk_info(self.root)
        if di and di.is_fat:
            big = [(it, [f for f in fs if f.size > FAT_LIMIT]) for it, fs in plan.entries]
            big = [(it, fs) for it, fs in big if fs]
            if big:
                for it, fs in big:
                    for f in fs:
                        rep.failed.append((f.path, "más de 4 GB: el disco de destino es FAT32 y no lo acepta"))
                    self.manifest.mark_failed(it, "más de 4 GB y el disco de destino es FAT32 (formatealo en exFAT o NTFS)")
                skip = {id(it) for it, _ in big}
                plan.entries = [(it, fs) for it, fs in plan.entries if id(it) not in skip]
                rep.warnings.append(f"{len(big)} elemento(s) pesan más de 4 GB y el disco es FAT32: no se copiaron.")
        self.manifest.set_meta("job_state", "running")
        self.manifest.set_meta("job_started", datetime.now().isoformat(timespec="seconds"))
        self._items_total = len(plan.entries)
        self._n_files = plan.file_count
        self._bytes_total = plan.total_bytes
        self._bytes_before = 0
        self._index = 0
        self._copied_rels: list[str] = []

        try:
            for item, files in plan.entries:
                errors: list[str] = []
                for f in files:
                    self._check_cancel()
                    self._index += 1
                    try:
                        copied = self._copy_with_retries(item, f)
                        if copied:
                            rep.copied_files += 1
                            rep.copied_bytes += f.size
                        else:
                            rep.skipped_files += 1
                    except (Cancelled, SourceUnavailable):
                        raise
                    except Exception as e:  # noqa: BLE001
                        msg = e.original if isinstance(e, SourceError) else e
                        errors.append(f"{f.name}: {msg}")
                        rep.failed.append((f.path, str(msg)))
                        self.log(f"ERROR {f.path}: {msg}")
                    self._bytes_before += f.size
                if not errors:
                    if self.opt.write_sidecar_json:
                        self._write_sidecar(item)
                    self.manifest.mark_done(item)
                    self.manifest.resolve_pending_by_fingerprint(item.fingerprint, item.source)
                    rep.items_done += 1
                else:
                    self.manifest.mark_failed(item, "; ".join(errors))
                self._emit("", force=True)
            self._make_compatible_copies()
        except Cancelled:
            rep.cancelled = True
        except SourceUnavailable as e:
            rep.interrupted = str(e)
            self.log(f"INTERRUMPIDO: {e}")
        finally:
            rep.seconds = time.monotonic() - start
            state = "cancelled" if rep.cancelled else ("interrupted" if rep.interrupted else "finished")
            self.manifest.set_meta("job_state", state)
            self.manifest.set_meta("job_finished", datetime.now().isoformat(timespec="seconds"))
        return rep

    # ----------------------------------------------------------------- interno
    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _wait_if_paused(self, name: str):
        if not self.pause.is_set():
            return
        self._state = STATE_PAUSED
        self._emit(name, force=True)
        while self.pause.is_set():
            self._check_cancel()
            time.sleep(0.2)
        self._state = STATE_COPYING
        self._speed.reset()

    def _emit(self, name: str, written: int = 0, force: bool = False):
        now = time.monotonic()
        if not force and now - getattr(self, "_last_emit", 0) < 0.2:
            return
        self._last_emit = now
        done = self._bytes_before + written
        self._speed.add(now, done)
        bps = self._speed.bps()
        eta = (self._bytes_total - done) / bps if bps > 0 else None
        self.progress_cb(Progress(
            file_index=self._index, file_count=self._n_files, bytes_done=done, bytes_total=self._bytes_total,
            current=name, speed_bps=bps, eta_seconds=eta, items_done=self.report.items_done,
            items_total=self._items_total, errors=len(self.report.failed), warnings=len(self.report.warnings),
            state=self._state, message=self._message))

    def _dest_for(self, item: MediaItem, f: RemoteFile) -> str:
        rel = local_relpath(item, f, self.opt.layout)
        n = 1
        candidate = rel
        while True:
            owner = self.manifest.local_path_owner(candidate)
            if owner == f.path:
                return candidate
            if owner is None and not (self.root / candidate).exists():
                return candidate
            n += 1
            candidate = with_suffix_number(rel, n)

    def _identical_orphan(self, item: MediaItem, f: RemoteFile, digest: str):
        """Busca, entre el nombre base y sus variantes _2, _3…, un archivo NO registrado e idéntico."""
        base = local_relpath(item, f, self.opt.layout)
        for n in range(1, 50):
            rel = base if n == 1 else with_suffix_number(base, n)
            p = self.root / rel
            if not p.exists():
                if n > 1:
                    break
                continue
            if self.manifest.local_path_owner(rel) is not None or p.stat().st_size != f.size:
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
                    h.update(block)
            if h.hexdigest() == digest:
                return rel, p
        return None

    def _copy_with_retries(self, item: MediaItem, f: RemoteFile) -> bool:
        if self.manifest.is_file_done(f.path, f.size):
            return False
        attempts = 0
        while True:
            try:
                self._copy_one(item, f)
                return True
            except Cancelled:
                raise
            except OSError as e:
                if _is_disk_full(e):
                    raise DestinationFull("El disco de destino se llenó. Lo copiado y verificado quedó guardado; "
                                          "liberá espacio (o elegí otro disco) y tocá «Copiar ahora».") from e
                attempts += 1
                if attempts > MAX_RETRIES:
                    raise
                self.log(f"Reintentando {f.name} ({attempts}/{MAX_RETRIES}): {e}")
                time.sleep(1)
            except SourceError as e:
                if type(e.original).__name__ == "PhoneDataBlocked":
                    raise SourceUnavailable(str(e.original)) from e
                if self.backend.is_connection_error(e.original):
                    self._recover(f.name)
                    continue   # retoma el mismo archivo desde el .part
                attempts += 1
                if attempts > MAX_RETRIES:
                    raise
                self.log(f"Reintentando {f.name} ({attempts}/{MAX_RETRIES}): {e}")
                time.sleep(1)
            except Exception as e:  # noqa: BLE001
                attempts += 1
                if attempts > MAX_RETRIES:
                    raise
                self.log(f"Reintentando {f.name} ({attempts}/{MAX_RETRIES}): {e}")
                time.sleep(1)

    def _recover(self, name: str):
        self._state = STATE_WAITING

        def status(msg: str):
            self._message = msg
            self.log(msg)
            self._emit(name, force=True)

        ok = self.backend.recover(status, self.cancel)
        self._check_cancel()
        if not ok:
            raise SourceUnavailable("La conexión no volvió a tiempo. Lo copiado quedó guardado; "
                                    "cuando vuelvas a conectar, tocá «Copiar» y sigue donde quedó.")
        self.report.reconnections += 1
        self._state = STATE_COPYING
        self._message = ""
        self._speed.reset()

    def _source_chunks(self, f: RemoteFile, offset: int):
        """Lee el origen en un hilo aparte con un búfer de PREFETCH bloques: mientras se escribe y se
        calcula el SHA-256 de un bloque, ya se está bajando el siguiente (cable/red nunca quedan ociosos).
        Memoria acotada: PREFETCH × 4 MB."""
        q: queue.Queue = queue.Queue(maxsize=PREFETCH)
        stop = threading.Event()

        def put(item) -> bool:
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def reader():
            it = None
            try:
                it = self.backend.read_chunks(f.path, CHUNK_SIZE, offset)
                for chunk in it:
                    if not put(("data", chunk)):
                        return
                put(("end", None))
            except BaseException as e:  # noqa: BLE001
                put(("err", e))
            finally:
                if it is not None:
                    try:
                        it.close()
                    except Exception:  # noqa: BLE001
                        pass

        th = threading.Thread(target=reader, name="lector-origen", daemon=True)
        th.start()
        try:
            while True:
                kind, val = q.get()
                if kind == "data":
                    yield val
                elif kind == "end":
                    return
                else:
                    raise SourceError(val) from val
        finally:
            stop.set()
            th.join(timeout=10)

    def _copy_one(self, item: MediaItem, f: RemoteFile) -> None:
        rel = self._dest_for(item, f)
        final = self.root / rel
        ensure_inside(self.root, final.parent)
        final.parent.mkdir(parents=True, exist_ok=True)
        ensure_inside(self.root, final)
        part = final.with_name(final.name + ".part")
        label = f"{f.name} ({ROLE_LABEL.get(f.role, f.role)})"
        h = hashlib.sha256()

        offset = 0
        if part.exists():
            existing = part.stat().st_size
            if 0 < existing < f.size:
                with open(part, "rb") as fh:          # retomamos: el hash incluye lo ya escrito
                    for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
                        h.update(block)
                offset = existing
                self.report.resumed_files += 1
                self.log(f"Retomando {f.name} desde el byte {existing:,}".replace(",", "."))
            else:
                part.unlink()

        written = offset
        try:
            with open(part, "ab" if offset else "wb") as out:
                for chunk in self._source_chunks(f, offset):
                    self._check_cancel()
                    self._wait_if_paused(label)
                    if written + len(chunk) > f.size:
                        # el origen manda más bytes de los anunciados: se corta ya (no llenar el disco)
                        raise VerificationError(f"el origen envió más datos de los esperados ({f.size} B)")
                    out.write(chunk)
                    h.update(chunk)
                    written += len(chunk)
                    self._emit(label, written)
                out.flush()
                os.fsync(out.fileno())
            if written != f.size:
                raise VerificationError(f"tamaño distinto (origen {f.size} B, copiado {written} B)")
            digest = h.hexdigest()
            if self.opt.verify_reread:
                check = hashlib.sha256()
                with open(part, "rb") as fh:
                    for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
                        check.update(block)
                if check.hexdigest() != digest:
                    raise VerificationError("el SHA-256 releído del disco no coincide")
            self._verify_on_device(f, digest)
            reuse = self._identical_orphan(item, f, digest)
            if reuse is not None:
                # Un corte justo entre «renombrar» y «anotar en el catálogo» dejó el archivo final ya
                # escrito pero sin registrar: se reconoce por SHA-256 y se reutiliza (sin duplicados).
                part.unlink(missing_ok=True)
                rel, final = reuse
                self.log(f"Ya estaba completo de una corrida anterior: {rel}")
            else:
                os.replace(part, final)
        except SourceError as e:
            # Corte de conexión o pausa por datos móviles: dejamos el .part para retomar.
            if not (self.backend.is_connection_error(e.original) or
                    type(e.original).__name__ == "PhoneDataBlocked"):
                part.unlink(missing_ok=True)
            raise
        except Cancelled:
            raise   # se conserva el .part: la próxima vez se retoma
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        validation = "sin validar"
        if self.opt.verify_structure:
            res = validate_file(final)
            validation = res.status
            if res.failed:
                msg = (f"{f.name}: estructura inusual ({res.detail}). La copia es idéntica al original "
                       "(mismo tamaño y SHA-256), así que se conserva.")
                self.report.warnings.append(msg)
                self.log("AVISO " + msg)

        if self.opt.preserve_dates and item.capture_date:
            try:
                set_file_times(final, item.capture_date)
            except OSError:
                pass
        self.manifest.record_file(item.key, f.path, f.role, f.size, digest, rel, validation)
        self._emit(label, written, force=True)
        self.log(f"OK  {f.path}  →  {rel}  (sha256 {digest[:12]}…, estructura {validation})")
        self._mirror(final, rel, digest)
        self._copied_rels.append(rel)

    def _verify_on_device(self, f: RemoteFile, digest: str) -> None:
        """Prueba de punta a punta (ADB): el SHA-256 calculado EN el celular tiene que ser el mismo que el de
        la copia. Si el celular no sabe calcularlo, alcanza con las otras verificaciones."""
        fn = getattr(self.backend, "remote_sha256", None)
        if fn is None or not getattr(self.opt, "verify_on_device", True):
            return
        try:
            remote = fn(f.path)
        except Exception as e:  # noqa: BLE001
            if self.backend.is_connection_error(e):
                raise SourceError(e) from e
            self.log(f"(no se pudo calcular el SHA-256 en el celular para {f.name}: {type(e).__name__})")
            return
        if remote and remote.lower() != digest:
            raise VerificationError("el SHA-256 calculado en el celular no coincide con la copia")
        if remote:
            self.report.device_verified += 1

    def _make_compatible_copies(self):
        """Fase 2: copias JPG/MP4 para abrir en Windows (los originales no se tocan)."""
        if not (self.opt.compat_photos or self.opt.compat_videos) or not self._copied_rels:
            return
        from .compat import convert_existing
        self._state = STATE_CONVERTING

        def prog(i, n, name):
            self._message = f"Creando copias compatibles con Windows (JPG/MP4): {i} de {n}"
            self._emit(name, written=0, force=True)

        res = convert_existing(self.manifest, self.opt.compat_videos, prog, self.cancel,
                               photos=self.opt.compat_photos, only=list(self._copied_rels))
        self.report.compat_created += res.created
        self.report.compat_failed += res.failed
        for rel, err in res.failed:
            self.log(f"No se pudo crear la copia compatible de {rel}: {err}")
        self._state = STATE_COPYING
        self._message = ""
        if res.cancelled:
            raise Cancelled()

    def _mirror(self, final: Path, rel: str, digest: str):
        for root in self.opt.mirror_destinations or []:
            if not root:
                continue
            try:
                if self.manifest.mirror_has(root, rel, digest):
                    continue
                target = Path(root) / rel
                ensure_inside(Path(root), target.parent)
                target.parent.mkdir(parents=True, exist_ok=True)
                ensure_inside(Path(root), target)
                tmp = target.with_name(target.name + ".part")
                shutil.copyfile(final, tmp)
                check = hashlib.sha256()
                with open(tmp, "rb") as fh:
                    for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
                        check.update(block)
                if check.hexdigest() != digest:
                    tmp.unlink(missing_ok=True)
                    raise VerificationError("SHA-256 distinto en la copia espejo")
                os.replace(tmp, target)
                shutil.copystat(final, target)
                self.manifest.record_mirror(root, rel, digest)
            except Exception as e:  # noqa: BLE001
                msg = f"No se pudo replicar {rel} en {root}: {e}"
                self.report.warnings.append(msg)
                self.log("AVISO " + msg)

    def _write_sidecar(self, item: MediaItem) -> None:
        rec = self.manifest.file_record(item.files[0].path) if item.files else None
        if not rec:
            return
        target = (self.root / rec["local_path"])
        sidecar = target.with_name(target.name + ".json")
        files = []
        for f in item.files:
            r = self.manifest.file_record(f.path)
            if r:
                files.append({"origen": f.path, "local": r["local_path"], "role": f.role,
                              "size": r["size"], "sha256": r["sha256"]})
        data = {
            "uuid": item.uuid,
            "source": item.source,
            "original_filename": item.original_filename,
            "path_en_origen": item.primary_path,
            "kind": item.kind,
            "capture_date": item.capture_date.isoformat() if item.capture_date else None,
            "live_photo": item.is_live,
            "screenshot": item.is_screenshot,
            "favorite": item.is_favorite,
            "hidden": item.is_hidden,
            "edited": item.has_edits,
            "albums": item.albums,
            "files": files,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
        }
        ensure_inside(self.root, sidecar)
        tmp = sidecar.with_name(sidecar.name + ".part")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, sidecar)
