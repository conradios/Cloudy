"""Batería de pruebas de estrés de iPhone Vault.

    python stress/run_stress.py            # todo
    python stress/run_stress.py seguridad  # solo una categoría (seguridad | rendimiento | fallas)

Cada prueba devuelve OK/FALLA + métricas. Al final se guarda stress/results.json.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

WORK = Path(tempfile.gettempdir()) / "iv_stress"
os.environ.setdefault("XDG_CONFIG_HOME", str(WORK / "config"))

from synthetic import Faults, SyntheticBackend, SyntheticLibrary  # noqa: E402

from iphone_vault.catalog.manifest import STATUS_DONE, Manifest  # noqa: E402
from iphone_vault.config import TransferOptions  # noqa: E402
from iphone_vault.device.base import DeviceBackend  # noqa: E402
from iphone_vault.models import AVAIL_LOCAL, ROLE_ORIGINAL, DeviceInfo, MediaItem, RemoteFile, RemoteStat  # noqa: E402
from iphone_vault.scanner.media_scanner import MediaScanner  # noqa: E402
from iphone_vault.security import no_network  # noqa: E402
from iphone_vault.transfer import engine as engine_mod  # noqa: E402
from iphone_vault.transfer.engine import TransferEngine, build_plan  # noqa: E402

# Las pruebas de estrés miden la COPIA; la conversión a JPG/MP4 se prueba aparte (tests/test_pipeline.py).
engine_mod.TransferEngine._make_compatible_copies = lambda self: None

RESULTS: list[dict] = []
MB = 1024 * 1024


def fresh(name: str) -> Path:
    p = WORK / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    return p


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(4 * MB), b""):
            h.update(b)
    return h.hexdigest()


def test(category: str, name: str):
    def deco(fn):
        fn._meta = (category, name)
        return fn
    return deco


def verify_against_source(m: Manifest, lib: SyntheticLibrary) -> tuple[int, list[str]]:
    """Cada archivo registrado como copiado tiene que ser idéntico al original."""
    bad = []
    n = 0
    for rec in m.all_files():
        vf = lib.files.get(rec["remote_path"])
        local = m.root / rec["local_path"]
        if vf is None or not local.exists():
            bad.append(f"falta {rec['local_path']}")
            continue
        got = sha_file(local)
        if got != rec["sha256"] or got != vf.sha256():
            bad.append(f"distinto {rec['local_path']}")
        n += 1
    finals = [p for p in m.root.rglob("*") if p.is_file() and ".vault" not in p.parts
              and not p.name.endswith((".json", ".part"))]
    registered = {str((m.root / r["local_path"]).resolve()) for r in m.all_files()}
    for p in finals:
        if str(p.resolve()) not in registered:
            bad.append(f"archivo final sin registrar (posible copia a medias): {p.name}")
    return n, bad


def run_copy(backend, dest: Path, opt: TransferOptions = None, **kw):
    opt = opt or TransferOptions(limit=0, write_sidecar_json=False)
    m = Manifest(dest)
    res = MediaScanner(backend).scan()
    plan = build_plan(res.items, opt, m)
    eng = TransferEngine(backend, m, opt, **kw)
    t = time.monotonic()
    rep = eng.run(plan)
    return m, plan, rep, time.monotonic() - t


# =================================================================== SEGURIDAD
@test("seguridad", "Red: solo conexiones locales durante todo el flujo + intentos de fuga bloqueados")
def t_network():
    events = []

    def hook(ev, args):
        if ev in ("socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.sendmsg"):
            events.append((ev, repr(args[1] if ev != "socket.getaddrinfo" else args[0])[:80]))

    sys.addaudithook(hook)
    no_network.install()
    no_network.allow_icloud(False)
    # flujo completo: iPhone demo + iCloud demo + copia + liberar espacio
    from iphone_vault.device.demo import DemoBackend, build_demo_library
    from iphone_vault.device.icloud_demo import DemoICloudAccount, DemoICloudBackend
    from iphone_vault.transfer.cleanup import delete_from_icloud, find_candidates
    from iphone_vault.config import CleanupRules
    root = build_demo_library(fresh("net_demo"))
    b = DemoBackend(root, slow=False)
    b.connect()
    dest = fresh("net_dest")
    m, plan, rep, _ = run_copy(b, dest)
    acc = DemoICloudAccount("x@icloud.com")
    acc.login("x")
    acc.submit_code("123456")
    cb = DemoICloudBackend(acc, root)
    items = cb.list_items()
    TransferEngine(cb, m, TransferOptions(limit=0)).run(build_plan(items, TransferOptions(limit=0), m))
    cands = find_candidates(cb.list_items(), m, CleanupRules(min_size_mb=1, only_videos=False, older_than_days=0))
    delete_from_icloud(cb, m, cands, 5, lambda *a: None, threading.Event())

    flow_events = list(events)
    attempts = {}

    def attempt(name, fn):
        try:
            fn()
            attempts[name] = "NO BLOQUEADO"
        except no_network.NetworkBlockedError:
            attempts[name] = "bloqueado"
        except Exception as e:  # noqa: BLE001
            attempts[name] = f"bloqueado ({type(e).__name__})" if "bloquead" in str(e).lower() else \
                f"otro error: {type(e).__name__}: {str(e)[:60]}"

    import urllib.request
    attempt("urllib a example.com", lambda: urllib.request.urlopen("http://example.com", timeout=3))
    import requests
    attempt("requests a example.com", lambda: requests.get("https://example.com", timeout=3))
    attempt("socket TCP a IP pública", lambda: socket.create_connection(("93.184.216.34", 80), timeout=3))
    attempt("DNS de google.com", lambda: socket.getaddrinfo("google.com", 443))
    attempt("UDP sendto 8.8.8.8 (DNS exfil)",
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("8.8.8.8", 53)))
    attempt("asyncio a example.com", lambda: asyncio.run(asyncio.open_connection("example.com", 80)))
    attempt("asyncio a IP pública", lambda: asyncio.run(asyncio.wait_for(asyncio.open_connection("1.1.1.1", 80), 3)))

    def raw():
        import _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(("1.1.1.1", 80))
        finally:
            s.close()

    attempt("_socket crudo (saltea el parche)", raw)
    no_network.allow_icloud(True)
    attempt("con iCloud activo: example.com", lambda: socket.getaddrinfo("example.com", 443))
    attempt("con iCloud activo: IP no-Apple", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3))
    no_network.allow_icloud(False)
    external = [e for e in flow_events if not any(x in e[1] for x in ("127.0.0.1", "localhost", "'/", "::1"))]
    ok = all(v.startswith("bloqueado") for v in attempts.values()) and not external
    return ok, {"eventos de red durante el flujo": len(flow_events), "externos": external[:5], "intentos": attempts}


@test("seguridad", "Datos: no quedan archivos fuera del destino (normal y tras kill -9 en pleno escaneo)")
def t_leaks():
    home, tmp = fresh("leak_home"), fresh("leak_tmp")
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp),
               XDG_CONFIG_HOME=str(home / ".config"), PYTHONPATH=str(ROOT / "src") + os.pathsep + str(HERE))
    dest = fresh("leak_dest")

    def snapshot():
        out = set()
        for base in (home, tmp):
            for p in base.rglob("*"):
                if p.is_file():
                    out.add(str(p))
        return out

    # 1) copia normal completa (sintética, con Photos.sqlite)
    code = (f"import sys;sys.argv=['x'];from stress_worker import main;main('{dest}', 30, 0, 0)")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(HERE), capture_output=True, text=True,
                       timeout=300)
    after_normal = snapshot()
    # 2) matar el proceso justo mientras copia la base de Fotos
    code2 = (f"from stress_worker import main_slow_scan;main_slow_scan('{fresh('leak_dest2')}')")
    p = subprocess.Popen([sys.executable, "-c", code2], env=env, cwd=str(HERE))
    time.sleep(2.5)
    p.kill()
    p.wait()
    after_kill = snapshot()
    # 3) arrancar de nuevo (la app limpia temporales viejos al iniciar)
    code3 = "from iphone_vault.security.cleanup_tmp import purge_stale;print(purge_stale())"
    r3 = subprocess.run([sys.executable, "-c", code3], env=env, cwd=str(HERE), capture_output=True, text=True)
    after_restart = snapshot()
    leaked_db = [x for x in after_kill if "Photos.sqlite" in x]
    leaked_after_restart = [x for x in after_restart if "Photos.sqlite" in x]
    ok = r.returncode == 0 and not after_normal and not leaked_after_restart
    return ok, {"archivos fuera del destino tras copia normal": sorted(after_normal)[:10],
                "copias de Photos.sqlite tras kill -9": len(leaked_db),
                "tras reiniciar la app": len(leaked_after_restart),
                "stderr": (r.stderr or "")[-300:] if r.returncode else "", "purga": (r3.stdout or r3.stderr)[-200:]}


@test("seguridad", "Contraseña de Apple: nunca se escribe en disco ni en el registro (login real contra Apple)")
def t_password():
    import logging
    marker = "PW-MARKER-" + hashlib.sha1(os.urandom(8)).hexdigest()[:12]
    cfg = fresh("pw_cfg")
    os.environ["XDG_CONFIG_HOME"] = str(cfg)
    logs = []

    class H(logging.Handler):
        def emit(self, record):
            try:
                logs.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    h = H()
    logging.getLogger().addHandler(h)
    old = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    from iphone_vault.device.icloud import ICloudAccount
    from iphone_vault.device.base import DeviceError
    outcome = ""
    try:
        ICloudAccount("iphone-vault-stress-test@example.com").login(marker)
        outcome = "login aceptado (?)"
    except DeviceError as e:
        outcome = str(e)[:120]
    finally:
        logging.getLogger().removeHandler(h)
        logging.getLogger().setLevel(old)
        no_network.allow_icloud(False)
    found_files = []
    for p in cfg.rglob("*"):
        if p.is_file() and marker.encode() in p.read_bytes():
            found_files.append(str(p))
    found_logs = [x[:100] for x in logs if marker in x]
    files = [str(p.relative_to(cfg)) for p in cfg.rglob("*") if p.is_file()]
    os.environ["XDG_CONFIG_HOME"] = str(WORK / "config")
    return not found_files and not found_logs, {
        "respuesta de Apple": outcome, "archivos guardados": files,
        "contraseña encontrada en archivos": found_files, "contraseña en logs": found_logs,
        "líneas de log analizadas": len(logs)}


class MemBackend(DeviceBackend):
    """Origen mínimo en memoria para inyectar elementos con nombres/tamaños hostiles."""
    source = "icloud"

    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs

    def connect(self, on_status=None):
        return DeviceInfo("mem", "mem", "-", "mem")

    def close(self):
        pass

    def listdir(self, path):
        return []

    def stat(self, path):
        return RemoteStat(len(self.blobs[path]), False) if path in self.blobs else None

    def read_chunks(self, path, chunk_size=4 * MB, offset=0):
        data = self.blobs[path]
        for i in range(offset, len(data), MB):
            yield data[i:i + MB]


def mem_item(i: int, name: str, data: bytes, when=None) -> tuple[MediaItem, str]:
    path = f"icloud/asset{i}/original"
    it = MediaItem(key=f"icloud:asset{i}", primary_path=path, kind="photo",
                   files=[RemoteFile(path, len(data), ROLE_ORIGINAL, name)], availability=AVAIL_LOCAL,
                   capture_date=when or datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).astimezone(),
                   original_filename=name, display_name=name, source="icloud", uuid=f"asset{i}")
    return it, path


@test("seguridad", "Nombres maliciosos: path traversal, nombres reservados de Windows, symlinks")
def t_names():
    for stale in WORK.glob("*escape*"):
        stale.unlink()
    dest = fresh("names_dest")
    outside = fresh("names_outside")
    names = ["../../../../escape1.HEIC", "..\\..\\escape2.HEIC", "/etc/escape3.HEIC", "C:\\Windows\\escape4.HEIC",
             "CON.HEIC", "nul", "aux.jpg", "COM1.MOV", "LPT9.png", "foto. ", "a" * 400 + ".HEIC",
             "IMG\x00\x07.HEIC", "名前😀.HEIC", "..", ".", "", "sub/dir/escape5.HEIC", "normal.HEIC"]
    blobs, items = {}, []
    for i, n in enumerate(names):
        data = b"\xff\xd8\xff\xe0" + os.urandom(1000) + b"\xff\xd9"
        it, path = mem_item(i, n, data)
        items.append(it)
        blobs[path] = data
    # symlink: <dest>/2025 apunta FUERA del destino
    (dest / "2025").symlink_to(outside, target_is_directory=True)
    it, path = mem_item(999, "via_symlink.HEIC", b"x" * 100, datetime(2025, 5, 5, tzinfo=timezone.utc).astimezone())
    items.append(it)
    blobs[path] = b"x" * 100
    b = MemBackend(blobs)
    m = Manifest(dest)
    opt = TransferOptions(limit=0, write_sidecar_json=True, verify_structure=False)
    rep = TransferEngine(b, m, opt).run(build_plan(items, opt, m))
    root = dest.resolve()
    escaped = []
    for p in list(WORK.rglob("*escape*")) + list(outside.rglob("*")):
        if dest.resolve() not in p.resolve().parents:
            escaped.append(str(p))
    for rec in m.all_files():
        real = (dest / rec["local_path"]).resolve()
        if root not in real.parents:
            escaped.append(rec["local_path"])
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    bad_names = []
    for p in dest.rglob("*"):
        if ".vault" in p.parts:
            continue
        n = p.name
        if n.split(".")[0].upper() in reserved or n.endswith((" ", ".")) or len(n) > 200 or \
                any(c in n for c in '<>:"\\|?*') or any(ord(c) < 32 for c in n):
            bad_names.append(n[:40])
    ok = not escaped and not bad_names
    return ok, {"copiados": rep.copied_files, "errores": len(rep.failed), "fuera del destino": escaped[:6],
                "nombres inválidos en Windows": bad_names[:6],
                "symlink": [f[1][:90] for f in rep.failed if "via_symlink" in f[0] or "asset999" in f[0]]}


@test("seguridad", "Base de Fotos hostil o corrupta: no rompe el escaneo ni ejecuta SQL")
def t_hostile_db():
    lib = SyntheticLibrary(20)
    out = {}
    # 1) base corrupta
    lib._db = os.urandom(200_000)
    b = SyntheticBackend(lib)
    b.connect()
    try:
        r = MediaScanner(b).scan()
        out["corrupta"] = f"ok: {len(r.items)} elementos, aviso: {bool(r.warnings)}"
    except Exception as e:  # noqa: BLE001
        out["corrupta"] = f"EXCEPCIÓN {type(e).__name__}: {e}"
    # 2) base con nombres de tabla maliciosos y datos basura
    tmp = Path(tempfile.mkdtemp()) / "h.sqlite"
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT, ZFILENAME TEXT, ZKIND,
          ZKINDSUBTYPE, ZDATECREATED, ZTRASHEDSTATE, ZHIDDEN, ZFAVORITE, ZHASADJUSTMENTS, ZSAVEDASSETTYPE);
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER);
        CREATE TABLE "Z_1ASSETS"" ; DROP TABLE ZASSET; --" (Z_1ALBUMS INTEGER, Z_1ASSETS INTEGER);
        CREATE TABLE canary (x);
        INSERT INTO ZASSET VALUES (1,'U1','DCIM/100APPLE','../../../evil.HEIC','x','y','no-es-fecha',NULL,'',NULL,1,'z');
        INSERT INTO ZASSET VALUES (2,NULL,NULL,NULL,NULL,NULL,1e300,1,1,1,1,1);
        INSERT INTO ZASSET VALUES (3,'U3','DCIM/100APPLE','IMG_0001.HEIC',0,0,-1e20,0,0,0,0,3);
    """)
    con.commit()
    con.close()
    lib2 = SyntheticLibrary(5)
    lib2._db = tmp.read_bytes()
    b2 = SyntheticBackend(lib2)
    b2.connect()
    try:
        r = MediaScanner(b2).scan()
        names = [i.name for i in r.items]
        out["maliciosa"] = f"ok: {len(r.items)} elementos; db leída: {r.photos_db_available}"
        out["nombres"] = names[:8]
    except Exception as e:  # noqa: BLE001
        out["maliciosa"] = f"EXCEPCIÓN {type(e).__name__}: {e}"
    ok = all(not str(v).startswith("EXCEPCIÓN") for v in out.values())
    return ok, out


@test("seguridad", "Origen hostil: stream infinito o truncado no llena el disco ni deja archivos")
def t_hostile_stream():
    lib = SyntheticLibrary(6, fixed_size=3 * MB)
    paths = [p for p in lib.files if p.startswith("DCIM")]
    f = Faults(truncate_paths={paths[0]}, overflow_paths={paths[1]})
    b = SyntheticBackend(lib, f)
    b.connect()
    dest = fresh("hostile_dest")
    t = time.monotonic()
    done = {}

    def go():
        done["r"] = run_copy(b, dest)

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(60)
    if th.is_alive():
        biggest = max((p.stat().st_size for p in dest.rglob("*.part")), default=0)
        return False, {"resultado": "COLGADO: el stream infinito nunca terminó", "mayor .part (MB)": biggest // MB}
    m, plan, rep, _ = done["r"]
    parts = list(dest.rglob("*.part"))
    failed = {p for p, _ in rep.failed}
    n, bad = verify_against_source(m, lib)
    ok = paths[0] in failed and paths[1] in failed and not parts and not bad
    return ok, {"tiempo (s)": round(time.monotonic() - t, 1), "fallidos detectados": sorted(failed),
                ".part sobrantes": len(parts), "inconsistencias": bad[:3]}


@test("seguridad", "Dos instancias sobre la misma carpeta: la segunda se rechaza")
def t_instance_lock():
    dest = fresh("lock_dest")
    m1 = Manifest(dest)
    code = f"from iphone_vault.catalog.manifest import Manifest;Manifest(__import__('pathlib').Path('{dest}'));print('ABRIÓ')"
    r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                       capture_output=True, text=True, timeout=30)
    m1.close()
    r2 = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                        capture_output=True, text=True, timeout=30)
    second_blocked = "ABRIÓ" not in r.stdout
    reopen_ok = "ABRIÓ" in r2.stdout
    return second_blocked and reopen_ok, {"segunda instancia bloqueada": second_blocked,
                                          "mensaje": (r.stderr.strip().splitlines() or [""])[-1][:140],
                                          "se puede abrir al cerrar la primera": reopen_ok}


# ================================================================= RENDIMIENTO
@test("rendimiento", "Escala: escaneo y plan de 30.000 elementos (fototeca grande)")
def t_scale():
    N = 30_000
    t = time.monotonic()
    lib = SyntheticLibrary(N)
    gen = time.monotonic() - t
    b = SyntheticBackend(lib)
    b.connect()
    t = time.monotonic()
    res = MediaScanner(b).scan()
    scan_s = time.monotonic() - t
    calls = dict(b.calls)
    dest = fresh("scale_dest")
    m = Manifest(dest)
    t = time.monotonic()
    plan = build_plan(res.items, TransferOptions(limit=0), m)
    plan_empty = time.monotonic() - t
    # simulamos que ya estaba todo copiado (catálogo lleno) y medimos la sincronización incremental
    rows, items_rows = [], []
    for it, files in plan.entries:
        for f in files:
            lp = f"x/{f.path.replace('/', '_')}"
            rows.append((f.path, it.key, f.role, f.size, "0" * 64, lp, "", ""))
        items_rows.append((it.key, it.primary_path, it.kind, None, STATUS_DONE, None, None, "", "", it.fingerprint,
                           "usb"))
    (dest / "x").mkdir()
    for r in rows:
        (dest / r[5]).touch()
    with m._lock:
        m._con.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", rows)
        m._con.executemany("INSERT INTO items(key,primary_path,kind,capture_date,status,reason,expected_size,"
                           "first_seen,updated_at,fingerprint,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)", items_rows)
        m._con.commit()
    t = time.monotonic()
    plan2 = build_plan(res.items, TransferOptions(limit=0), m)
    plan_full = time.monotonic() - t
    # estimación con latencia real de USB (AFC ~1,5 ms por consulta, 32 en paralelo para stat)
    L = 0.0015
    est = calls["listdir"] * L + calls["stat"] * L / 32 + len(lib._db) / (35 * MB)
    ok = scan_s < 60 and plan_empty < 30 and plan_full < 30 and len(plan2.items) == 0
    return ok, {"elementos": len(res.items), "archivos": len(lib.files) - 1, "GB virtuales": round(lib.total_bytes / 1e9),
                "escaneo (s, sin latencia)": round(scan_s, 2), "escaneo estimado con USB real (s)": round(est, 1),
                "plan con catálogo vacío (s)": round(plan_empty, 2),
                "plan incremental con 30k ya copiados (s)": round(plan_full, 2),
                "nuevos detectados en 2.ª pasada": len(plan2.items), "llamadas al iPhone": calls,
                "tamaño Photos.sqlite (MB)": round(len(lib._db) / MB, 1), "generación (s)": round(gen, 1)}


@test("rendimiento", "Velocidad máxima del motor con un video de 2 GB (con y sin relectura) + memoria")
def t_throughput():
    size = 2 * 1024 * MB
    lib = SyntheticLibrary(0, big_video_bytes=size)
    b = SyntheticBackend(lib)
    b.connect()
    vf = lib.files["DCIM/999APPLE/IMG_9999.MOV"]
    t = time.monotonic()
    for off in range(0, size, 4 * MB):
        vf.read(off, 4 * MB)
    src_only = size / MB / (time.monotonic() - t)
    out = {"lectura del origen sintético (MB/s)": round(src_only)}
    import tracemalloc
    tracemalloc.start()
    for reread in (True, False):
        dest = fresh(f"tp_dest_{reread}")
        opt = TransferOptions(limit=0, verify_reread=reread, write_sidecar_json=False)
        m, plan, rep, secs = run_copy(b, dest, opt)
        out[f"motor {'con' if reread else 'sin'} relectura SHA-256 (MB/s)"] = round(size / MB / secs)
        out[f"ok {reread}"] = not rep.failed and rep.copied_bytes == size
        shutil.rmtree(dest)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out["memoria pico de Python durante 2 GB (MB)"] = round(peak / MB, 1)
    ok = out["ok True"] and out["ok False"] and out["memoria pico de Python durante 2 GB (MB)"] < 100
    return ok, out


@test("rendimiento", "Muchos archivos chicos: 3.000 archivos de 64 KB (costo fijo por archivo)")
def t_small_files():
    lib = SyntheticLibrary(3000, fixed_size=64 * 1024)
    b = SyntheticBackend(lib)
    b.connect()
    dest = fresh("small_dest")
    m, plan, rep, secs = run_copy(b, dest)
    nfiles = rep.copied_files
    return not rep.failed, {"archivos": nfiles, "segundos": round(secs, 1), "archivos/s": round(nfiles / secs),
                            "ms por archivo": round(secs / nfiles * 1000, 1)}


@test("rendimiento", "Velocidad real simulada (cable a 40 MB/s): el motor no frena y el tiempo restante es fiable")
def t_eta():
    lib = SyntheticLibrary(40, fixed_size=10 * MB)
    b = SyntheticBackend(lib, Faults(throughput_mbps=40))
    b.connect()
    dest = fresh("eta_dest")
    snaps = []

    def prog(p):
        snaps.append((time.monotonic(), p.bytes_done, p.bytes_total, p.eta_seconds, p.speed_bps))

    m, plan, rep, secs = run_copy(b, dest, progress=prog)
    end = time.monotonic()
    errs = []
    for ts, done, total, eta, _ in snaps:
        if eta and 0.2 < done / total < 0.8:
            errs.append(abs((end - ts) - eta) / (end - ts))
    eff = rep.copied_bytes / MB / secs
    err = sum(errs) / len(errs) * 100 if errs else 0
    return eff > 34 and err < 20, {"límite del cable (MB/s)": 40, "velocidad lograda (MB/s)": round(eff, 1),
                                   "error medio del tiempo restante (%)": round(err, 1),
                                   "muestras de progreso": len(snaps)}


@test("rendimiento", "Interfaz: refresco con 30.000 elementos y costo por actualización en vivo")
def t_gui():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    from iphone_vault.ui.gui import main_window as mw
    from iphone_vault.ui.gui import theme
    from iphone_vault.transfer.engine import Progress
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.QSS)
    lib = SyntheticLibrary(30_000)
    b = SyntheticBackend(lib)
    b.connect()
    res = MediaScanner(b).scan()
    w = mw.MainWindow()
    w.settings.destination = str(fresh("gui_dest"))
    w._open_manifest()
    w.usb = b
    w.usb_items = res.items
    t = time.monotonic()
    w._refresh_all()
    refresh = time.monotonic() - t
    w.syncing = True
    w.last_progress = Progress(5, 100, 50 * MB, 500 * MB, "IMG_0001.HEIC (original)", 40 * MB, 60, 3, 100)
    t = time.monotonic()
    for _ in range(20):
        w._on_progress(w.last_progress)
        app.processEvents()
    tick = (time.monotonic() - t) / 20
    w.syncing = False
    w.close()
    budget = 0.2   # se actualiza 5 veces por segundo
    return refresh < 5 and tick < budget * 0.5, {
        "refresco completo (s)": round(refresh, 2), "costo por actualización en vivo (ms)": round(tick * 1000, 1),
        "presupuesto por actualización (ms)": int(budget * 1000), "filas": len(w.rows)}


# ====================================================================== FALLAS
@test("fallas", "Cortes de cable aleatorios: 12+ desconexiones durante 500 MB")
def t_disconnects():
    lib = SyntheticLibrary(60, fixed_size=8 * MB)
    b = SyntheticBackend(lib, Faults(disconnect_every_bytes=37 * MB, disconnect_down_s=0.3))
    b.connect()
    dest = fresh("disc_dest")
    m, plan, rep, secs = run_copy(b, dest)
    n, bad = verify_against_source(m, lib)
    ok = not rep.failed and not bad and rep.reconnections >= 10 and rep.items_done == len(plan.items)
    return ok, {"desconexiones": b.calls["disconnects"], "reconexiones": rep.reconnections,
                "archivos retomados a mitad": rep.resumed_files, "verificados idénticos": n,
                "inconsistencias": bad[:3], "segundos": round(secs, 1)}


@test("fallas", "Lecturas con errores al azar (3 %): reintenta y nunca guarda basura")
def t_flaky():
    lib = SyntheticLibrary(80, fixed_size=2 * MB)
    b = SyntheticBackend(lib, Faults(flaky_read_prob=0.03))
    b.connect()
    dest = fresh("flaky_dest")
    m, plan, rep, _ = run_copy(b, dest)
    m2, plan2, rep2, _ = run_copy(b, dest)    # segunda pasada: reintenta lo que falló
    n, bad = verify_against_source(m2, lib)
    return not bad and n == plan.file_count, {
        "errores inyectados": b.calls["flaky"], "fallidos 1.ª pasada": len(rep.failed),
        "fallidos 2.ª pasada": len(rep2.failed), "verificados idénticos": n, "inconsistencias": bad[:3]}


@test("fallas", "Disco lleno a mitad de copia: no deja basura y la siguiente corrida completa")
def t_disk_full():
    lib = SyntheticLibrary(20, fixed_size=8 * MB)
    b = SyntheticBackend(lib)
    b.connect()
    dest = fresh("full_dest")
    real_open = open
    written = {"n": 0}

    class FullFile:
        def __init__(self, fh):
            self.fh = fh

        def write(self, data):
            written["n"] += len(data)
            if written["n"] > 50 * MB:
                raise OSError(28, "No space left on device")
            return self.fh.write(data)

        def __getattr__(self, k):
            return getattr(self.fh, k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.fh.__exit__(*a)

    def fake_open(p, mode="r", *a, **k):
        fh = real_open(p, mode, *a, **k)
        return FullFile(fh) if "a" in mode or "w" in mode else fh

    engine_mod.open = fake_open
    try:
        m, plan, rep, _ = run_copy(b, dest)
    finally:
        del engine_mod.open
    parts = list(dest.rglob("*.part"))
    m2, plan2, rep2, _ = run_copy(b, dest)
    n, bad = verify_against_source(m2, lib)
    ok = bool(rep.interrupted) and not parts and not rep2.failed and not bad and n == plan.file_count
    return ok, {"se detuvo enseguida": bool(rep.interrupted), "mensaje": (rep.interrupted or "")[:90],
                ".part sobrantes": len(parts), "2.ª corrida: verificados idénticos": n, "inconsistencias": bad[:3]}


@test("fallas", "Archivo bloqueado por el antivirus al renombrar (típico de Windows): reintenta")
def t_locked():
    lib = SyntheticLibrary(10, fixed_size=1 * MB)
    b = SyntheticBackend(lib)
    b.connect()
    dest = fresh("locked_dest")
    real = os.replace
    count = {"n": 0}

    def flaky_replace(a, c):
        count["n"] += 1
        if count["n"] % 3 == 1:
            raise PermissionError(13, "El proceso no tiene acceso al archivo porque está siendo utilizado")
        return real(a, c)

    engine_mod.os.replace = flaky_replace
    try:
        m, plan, rep, _ = run_copy(b, dest)
    finally:
        engine_mod.os.replace = real
    n, bad = verify_against_source(m, lib)
    return not rep.failed and not bad, {"bloqueos inyectados": (count["n"] + 2) // 3, "fallidos": len(rep.failed),
                                        "verificados idénticos": n}


@test("fallas", "kill -9 del proceso 8 veces en momentos al azar: retoma y todo queda íntegro")
def t_kill9():
    dest = fresh("kill_dest")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(HERE))
    kills = 0
    rnd = __import__("random").Random(5)
    for delay in [rnd.uniform(0.8, 3.0) for _ in range(8)]:
        p = subprocess.Popen([sys.executable, "-c", f"from stress_worker import main;main('{dest}', 40, 20, 0)"],
                             env=env, cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(delay)
        if p.poll() is None:
            p.kill()
            kills += 1
        p.wait()
    parts_after_kill = len(list(dest.rglob("*.part")))
    r = subprocess.run([sys.executable, "-c", f"from stress_worker import main;main('{dest}', 40, 0, 0)"],
                       env=env, cwd=str(HERE), capture_output=True, text=True, timeout=300)
    lib = SyntheticLibrary(40, seed=11, fixed_size=12 * MB)
    m = Manifest(dest)
    n, bad = verify_against_source(m, lib)
    with m._lock:
        integrity = m._con.execute("PRAGMA integrity_check").fetchone()[0]
    m.close()
    worker = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"err": r.stderr[-300:]}
    ok = kills >= 6 and not bad and integrity == "ok" and n == worker.get("planned_total", -1)
    return ok, {"procesos matados": kills, ".part que quedaron (se retoman)": parts_after_kill,
                "corrida final": worker, "verificados idénticos": n, "integridad del catálogo": integrity,
                "inconsistencias": bad[:3]}


@test("fallas", "El iPhone no vuelve nunca: se rinde a tiempo, conserva lo parcial y avisa")
def t_never_back():
    lib = SyntheticLibrary(10, fixed_size=8 * MB)
    b = SyntheticBackend(lib, Faults(disconnect_every_bytes=20 * MB, never_come_back=True))
    b.connect()
    dest = fresh("never_dest")
    t = time.monotonic()
    m, plan, rep, _ = run_copy(b, dest)
    secs = time.monotonic() - t
    parts = list(dest.rglob("*.part"))
    ok = bool(rep.interrupted) and secs < b.recover_timeout + 10 and len(parts) <= 1
    return ok, {"interrumpido": (rep.interrupted or "")[:90], "segundos": round(secs, 1),
                "elementos completos antes del corte": rep.items_done, ".part conservados": len(parts)}


@test("fallas", "Cancelar mientras espera la reconexión responde al instante")
def t_cancel_wait():
    lib = SyntheticLibrary(5, fixed_size=8 * MB)
    b = SyntheticBackend(lib, Faults(disconnect_every_bytes=10 * MB, never_come_back=True))
    b.recover_timeout = 600
    b.connect()
    dest = fresh("cancelw_dest")
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t = time.monotonic()
    m, plan, rep, _ = run_copy(b, dest, cancel_event=cancel)
    secs = time.monotonic() - t
    return rep.cancelled and secs < 6, {"cancelado": rep.cancelled, "segundos hasta cortar": round(secs, 1)}


@test("fallas", "iCloud: enlace vencido (403) + servidor que ignora Range + corte de red a mitad")
def t_icloud_faults():
    import requests
    from iphone_vault.device.icloud import ICloudBackend
    data = {f"A{i}": os.urandom(3 * MB + i) for i in range(4)}
    state = {"gets": 0, "expired_once": False, "cut_once": False, "relists": 0}

    class Res:
        def __init__(self, aid, n):
            self.url, self.size, self.filename = f"https://cvws.icloud-content.com/{aid}", n, f"IMG_{aid}.HEIC"

    class Asset:
        def __init__(self, aid):
            self.id, self.filename, self.item_type, self.size = aid, f"IMG_{aid}.HEIC", "image", len(data[aid])
            self.created = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.resources = {"original": Res(aid, len(data[aid]))}

        def download_url(self, v):
            return f"https://cvws.icloud-content.com/{self.id}?t={state['relists']}"

    class Resp:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def iter_content(self, chunk_size):
            for i in range(0, len(self.body), chunk_size):
                if not state["cut_once"] and i > MB and self.body is data["A2"]:
                    state["cut_once"] = True
                    raise requests.ConnectionError("conexión reseteada")
                yield self.body[i:i + chunk_size]

        def close(self):
            pass

    class Session:
        def get(self, url, stream, headers, timeout):
            state["gets"] += 1
            aid = url.rsplit("/", 1)[1].split("?")[0]
            if aid == "A1" and not state["expired_once"]:
                state["expired_once"] = True
                return Resp(403, b"")
            return Resp(200, data[aid])          # ignora el Range a propósito

    class Photos:
        session = Session()

        @property
        def all(self):
            state["relists"] += 1
            return [Asset(a) for a in data]

    class API:
        photos = Photos()

        def authenticate(self, force_refresh=False):
            pass

    class Acc:
        apple_id, api, is_demo = "x@icloud.com", API(), False

    b = ICloudBackend(Acc())
    b.is_present = lambda: True
    items = b.list_items()
    dest = fresh("icloud_dest")
    m = Manifest(dest)
    opt = TransferOptions(limit=0, verify_structure=False)
    rep = TransferEngine(b, m, opt).run(build_plan(items, opt, m))
    bad = []
    for aid, blob in data.items():
        rec = m.file_record(f"icloud/{aid}/original")
        if not rec or (dest / rec["local_path"]).read_bytes() != blob:
            bad.append(aid)
    ok = not rep.failed and not bad and state["expired_once"] and state["cut_once"]
    return ok, {"reconexiones": rep.reconnections, "retomados": rep.resumed_files, "relistados": state["relists"],
                "descargas HTTP": state["gets"], "archivos distintos": bad, "fallidos": rep.failed[:2]}


@test("fallas", "Nombres repetidos y archivos tuyos ya existentes: nunca se pisa nada")
def t_collisions():
    dest = fresh("coll_dest")
    (dest / "2026").mkdir(parents=True)
    mine = dest / "2026" / "IMG_0001.HEIC"
    mine.write_bytes(b"MI ARCHIVO PERSONAL")
    blobs, items = {}, []
    for i in range(3):
        data = os.urandom(5000 + i)
        it, path = mem_item(i, "IMG_0001.HEIC", data)
        items.append(it)
        blobs[path] = data
    zero, zpath = mem_item(50, "vacio.MOV", b"")
    items.append(zero)
    blobs[zpath] = b""
    b = MemBackend(blobs)
    m = Manifest(dest)
    opt = TransferOptions(limit=0, write_sidecar_json=False)
    rep = TransferEngine(b, m, opt).run(build_plan(items, opt, m))
    files = sorted(p.name for p in (dest / "2026").iterdir())
    ok = mine.read_bytes() == b"MI ARCHIVO PERSONAL" and len([f for f in files if f.startswith("IMG_0001")]) == 4 \
        and not rep.failed
    return ok, {"archivos en la carpeta": files, "tu archivo intacto": mine.read_bytes() == b"MI ARCHIVO PERSONAL",
                "fallidos": rep.failed, "advertencias": [w[:70] for w in rep.warnings]}


@test("fallas", "Interfaz: un error inesperado en segundo plano se muestra y la app sigue usable")
def t_gui_exception():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication, QMessageBox
    from iphone_vault.ui.gui import main_window as mw
    app = QApplication.instance() or QApplication([])
    shown = []
    orig = QMessageBox.critical
    QMessageBox.critical = staticmethod(lambda *a, **k: shown.append(a[2]))
    real_scan = mw.MediaScanner.scan

    def boom(self):
        raise RuntimeError("falla inesperada simulada")

    mw.MediaScanner.scan = boom
    try:
        w = mw.MainWindow()
        w.chk_demo.setChecked(True)
        w.settings.destination = str(fresh("guiexc_dest"))
        w._open_manifest()
        w._connect_usb()
        t = time.monotonic()
        while w.task and not w.task.completed and time.monotonic() - t < 20:
            app.processEvents()
            time.sleep(0.02)
        for _ in range(30):
            app.processEvents()
            time.sleep(0.01)
        mw.MediaScanner.scan = real_scan
        usable = w.btn_usb.isEnabled()
        w._connect_usb()     # segundo intento, ahora sin la falla
        t = time.monotonic()
        while w.task and not w.task.completed and time.monotonic() - t < 20:
            app.processEvents()
            time.sleep(0.02)
        for _ in range(30):
            app.processEvents()
            time.sleep(0.01)
        recovered = w.usb is not None and len(w.usb_items) > 0
        w.close()
    finally:
        mw.MediaScanner.scan = real_scan
        QMessageBox.critical = orig
    return bool(shown) and usable and recovered, {"mensaje mostrado": shown[:1], "botones habilitados": usable,
                                                  "segundo intento funcionó": recovered}


# ======================================================================= main
def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    WORK.mkdir(parents=True, exist_ok=True)
    tests = [v for v in globals().values() if callable(v) and hasattr(v, "_meta")]
    for fn in tests:
        cat, name = fn._meta
        if only and cat != only:
            continue
        t = time.monotonic()
        try:
            ok, metrics = fn()
        except Exception as e:  # noqa: BLE001
            ok, metrics = False, {"EXCEPCIÓN": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}
        secs = time.monotonic() - t
        RESULTS.append({"categoria": cat, "prueba": name, "ok": bool(ok), "segundos": round(secs, 1),
                        "metricas": metrics})
        print(f"[{'OK   ' if ok else 'FALLA'}] ({cat}) {name}  [{secs:.1f}s]")
        for k, v in metrics.items():
            print(f"        {k}: {v}")
        sys.stdout.flush()
        gc.collect()
    out = HERE / ("results.json" if not only else f"results_{only}.json")
    out.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    passed = sum(r["ok"] for r in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} pruebas OK")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
