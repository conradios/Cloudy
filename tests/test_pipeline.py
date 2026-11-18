"""Pruebas del flujo completo con el iPhone y el iCloud simulados.  Ejecutar:  python -m pytest -q"""
import hashlib
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iphone_vault.catalog.manifest import STATUS_PENDING, Manifest  # noqa: E402
from iphone_vault.config import LAYOUT_MIRROR, CleanupRules, TransferOptions  # noqa: E402
from iphone_vault.device.base import DisconnectedError  # noqa: E402
from iphone_vault.device.demo import DemoBackend, build_demo_library, simulate_originals_downloaded  # noqa: E402
from iphone_vault.device.icloud_demo import DemoICloudAccount, DemoICloudBackend  # noqa: E402
from iphone_vault.models import AVAIL_CLOUD_ONLY, AVAIL_OPTIMIZED  # noqa: E402
from iphone_vault.scanner.media_scanner import MediaScanner  # noqa: E402
from iphone_vault.security import no_network  # noqa: E402
from iphone_vault.transfer.cleanup import delete_from_icloud, find_candidates  # noqa: E402
from iphone_vault.transfer.engine import TransferEngine, build_plan, register_unavailable  # noqa: E402
from iphone_vault.transfer.validate import FAILED, OK, validate_file  # noqa: E402


@pytest.fixture
def root(tmp_path):
    return build_demo_library(tmp_path / "iphone")


@pytest.fixture
def phone(root):
    b = DemoBackend(root, slow=False)
    b.connect()
    return b


@pytest.fixture
def cloud(root):
    acc = DemoICloudAccount("demo@icloud.com")
    assert acc.login("x") == "2fa"
    assert acc.submit_code("123456")
    return DemoICloudBackend(acc, root)


def scan(b):
    return MediaScanner(b).scan()


def copy_all(backend, items, m, **kw):
    opt = TransferOptions(limit=0, **kw)
    plan = build_plan(items, opt, m)
    return TransferEngine(backend, m, opt).run(plan), plan


def test_scan_detects_everything(phone):
    res = scan(phone)
    by_name = {i.name: i for i in res.items}
    assert res.photos_db_available
    assert by_name["IMG_0013.HEIC"].availability == AVAIL_CLOUD_ONLY
    assert by_name["IMG_0014.MOV"].availability == AVAIL_CLOUD_ONLY
    assert by_name["IMG_0012.HEIC"].availability == AVAIL_OPTIMIZED
    assert by_name["IMG_0001.HEIC"].is_live and len(by_name["IMG_0001.HEIC"].files) == 2
    assert by_name["IMG_0004.HEIC"].has_edits
    assert by_name["IMG_0006.PNG"].is_screenshot
    assert by_name["IMG_0015.HEIC"].is_trashed
    assert by_name["IMG_0008.HEIC"].albums == ["Vacaciones"]
    assert res.warnings


def test_limit_and_incremental(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    register_unavailable(res.items, m)
    plan = build_plan(res.items, TransferOptions(limit=10), m)
    assert len(plan.items) == 10
    rep = TransferEngine(phone, m, TransferOptions()).run(plan)
    assert not rep.failed and rep.items_done == 10 and not rep.warnings
    plan2 = build_plan(res.items, TransferOptions(limit=0), m)
    assert plan2.already_done == 10
    assert {i.name for i in plan2.items} == {"IMG_0001.HEIC", "IMG_0002.HEIC", "IMG_0003.JPG"}


def test_copies_are_byte_identical_and_structure_ok(phone, tmp_path):
    dest = tmp_path / "dest"
    m = Manifest(dest)
    res = scan(phone)
    rep, _ = copy_all(phone, res.items, m, layout=LAYOUT_MIRROR)
    assert not rep.failed and not rep.warnings
    for it in res.local_items:
        if it.is_trashed:
            continue
        for f in it.files:
            if f.role == "aae":
                continue
            rec = m.file_record(f.path)
            local = dest / rec["local_path"]
            assert hashlib.sha256(local.read_bytes()).hexdigest() == hashlib.sha256(phone.read_all(f.path)).hexdigest()
            assert rec["validation"] == OK
    assert not list(dest.rglob("*.part"))


def test_pending_flow_via_iphone(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    assert register_unavailable(res.items, m) == 3
    assert not build_plan(res.items, TransferOptions(limit=0), m, only_pending=True).items
    simulate_originals_downloaded(phone.root)
    res2 = scan(phone)
    plan = build_plan(res2.items, TransferOptions(limit=0), m, only_pending=True)
    assert {i.name for i in plan.items} == {"IMG_0012.HEIC", "IMG_0013.HEIC", "IMG_0014.MOV"}
    TransferEngine(phone, m, TransferOptions(limit=0)).run(plan)
    assert not m.keys_with_status(STATUS_PENDING)


def test_pending_resolved_via_icloud_without_duplicates(phone, cloud, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    register_unavailable(res.items, m)
    copy_all(phone, res.items, m)
    citems = cloud.list_items()
    plan = build_plan(citems, TransferOptions(limit=0), m)
    # Solo lo que faltaba: los 3 pendientes + 2 que solo están en iCloud. Nada duplicado.
    assert {i.name for i in plan.items} == {"IMG_0012.HEIC", "IMG_0013.HEIC", "IMG_0014.MOV",
                                           "IMG_0018.HEIC", "IMG_0019.MOV"}
    rep = TransferEngine(cloud, m, TransferOptions(limit=0)).run(plan)
    assert not rep.failed
    assert not m.keys_with_status(STATUS_PENDING)
    assert not build_plan(citems, TransferOptions(limit=0), m).items
    # y el iPhone tampoco vuelve a copiar nada
    assert not build_plan(scan(phone).items, TransferOptions(limit=0), m).items


def test_resume_after_cable_unplugged(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    opt = TransferOptions(limit=0)
    plan = build_plan(res.items, opt, m)
    phone.simulate_unplug(after_bytes=5_000_000, replug_after_seconds=0.5)
    msgs = []
    rep = TransferEngine(phone, m, opt, log_cb=msgs.append).run(plan)
    assert rep.reconnections == 1 and rep.resumed_files == 1
    assert not rep.failed and rep.items_done == len(plan.items)
    assert any("Retomando" in x for x in msgs)
    for it, files in plan.entries:
        for f in files:
            rec = m.file_record(f.path)
            data = (m.root / rec["local_path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == hashlib.sha256(phone.read_all(f.path)).hexdigest()


def test_cancel_keeps_part_and_next_run_resumes(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    opt = TransferOptions(limit=1, include_photos=False)   # un video de ~6 MB
    plan = build_plan(res.items, opt, m)
    cancel = threading.Event()

    class Slow(DemoBackend):
        def read_chunks(self, path, chunk_size=1024 * 1024, offset=0):
            import time
            for c in super().read_chunks(path, 256 * 1024, offset):
                time.sleep(0.1)
                yield c

    slow = Slow(phone.root, slow=False)
    slow.connect()

    def progress(p):
        if p.bytes_done > 1_500_000:
            cancel.set()

    rep = TransferEngine(slow, m, opt, progress=progress, cancel_event=cancel).run(plan)
    assert rep.cancelled
    parts = list(m.root.rglob("*.part"))
    assert len(parts) == 1 and parts[0].stat().st_size > 0
    rep2 = TransferEngine(phone, m, opt).run(build_plan(res.items, opt, m))
    assert rep2.resumed_files == 1 and rep2.items_done == 1 and not list(m.root.rglob("*.part"))


def test_source_read_error_leaves_no_partial_file(phone, tmp_path):
    class Broken(DemoBackend):
        def read_chunks(self, path, chunk_size=1024 * 1024, offset=0):
            gen = super().read_chunks(path, 64 * 1024, offset)
            yield next(gen)
            raise ValueError("dato corrupto en el origen")

    b = Broken(phone.root, slow=False)
    b.connect()
    m = Manifest(tmp_path / "dest")
    res = scan(b)
    opt = TransferOptions(limit=1)
    rep = TransferEngine(b, m, opt).run(build_plan(res.items, opt, m))
    assert rep.failed and rep.copied_files == 0
    assert [p for p in m.root.rglob("*") if p.is_file() and ".vault" not in p.parts] == []


def test_structure_validator(tmp_path):
    good = tmp_path / "a.HEIC"
    good.write_bytes(b"\x00\x00\x00\x10ftypheic\x00\x00\x00\x00" + b"\x00\x00\x00\x08meta" + b"\x00\x00\x00\x0cmdat1234")
    assert validate_file(good).status == OK
    bad = tmp_path / "b.HEIC"
    bad.write_bytes(good.read_bytes()[:-3])
    assert validate_file(bad).status == FAILED
    jpg = tmp_path / "c.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
    assert validate_file(jpg).status == FAILED


def test_mirror_destination(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    mirror = tmp_path / "nas"
    res = scan(phone)
    opt = TransferOptions(limit=3, mirror_destinations=[str(mirror)])
    rep = TransferEngine(phone, m, opt).run(build_plan(res.items, opt, m))
    assert not rep.warnings
    for p in (m.root).rglob("*"):
        if p.is_file() and ".vault" not in p.parts and not p.name.endswith(".json"):
            assert (mirror / p.relative_to(m.root)).read_bytes() == p.read_bytes()


def test_cleanup_only_deletes_verified_full_quality(phone, cloud, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    register_unavailable(res.items, m)
    copy_all(phone, res.items, m)                       # sin los 3 pendientes
    citems = cloud.list_items()
    rules = CleanupRules(min_size_mb=1, only_videos=False, older_than_days=0, batch_size=100)
    cands = find_candidates(citems, m, rules)
    names = {c.item.name for c in cands}
    assert "IMG_0012.HEIC" not in names and "IMG_0013.HEIC" not in names   # no hay copia completa local
    assert "IMG_0005.MOV" in names
    # si la copia local se corrompe, NO se borra
    victim = next(c for c in cands if c.item.name == "IMG_0005.MOV")
    p = m.root / victim.local_files[0]["local_path"]
    data = bytearray(p.read_bytes())
    data[100] ^= 0xFF
    p.write_bytes(bytes(data))
    rep = delete_from_icloud(cloud, m, cands, 100, lambda *a: None, threading.Event())
    assert ("IMG_0005.MOV" in {n for n, _ in rep.skipped})
    assert rep.deleted == len(cands) - 1
    assert "IMG_0005.MOV" in {i.name for i in cloud.list_items()}


def test_network_is_blocked_except_apple_when_enabled():
    no_network.install()
    no_network.allow_icloud(False)
    with pytest.raises(no_network.NetworkBlockedError):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    with pytest.raises(no_network.NetworkBlockedError):
        socket.getaddrinfo("www.icloud.com", 443)
    no_network.allow_icloud(True)
    with pytest.raises(no_network.NetworkBlockedError):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(no_network.NetworkBlockedError):
        socket.socket().connect(("8.8.8.8", 53))
    no_network.allow_icloud(False)
    # loopback a un puerto cualquiera: bloqueado (evita «saltar» por un proxy local)
    with pytest.raises(no_network.NetworkBlockedError):
        socket.socket().connect(("127.0.0.1", 9))
    # loopback a un puerto abierto por este mismo proceso (pares internos de asyncio/Qt): permitido
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.socket()
    c.connect(srv.getsockname())
    c.close()
    srv.close()


def test_disconnected_error_is_connection_error(phone):
    assert phone.is_connection_error(DisconnectedError("x"))
    assert not phone.is_connection_error(ValueError("x"))


def test_phone_hotspot_blocks_icloud_but_not_cable(phone, tmp_path, monkeypatch):
    """Si la PC sale a internet por el Punto de acceso del iPhone, iCloud se frena; el cable sigue."""
    from iphone_vault.device import icloud as icloud_mod
    from iphone_vault.security import hotspot
    no_network.install()
    monkeypatch.setattr(hotspot, "using_phone_data", lambda: True)
    no_network.allow_icloud(True)
    no_network.allow_phone_data(False)
    try:
        with pytest.raises(no_network.NetworkBlockedError):
            socket.getaddrinfo("www.icloud.com", 443)
        with pytest.raises(icloud_mod.PhoneDataBlocked):
            icloud_mod.ICloudAccount("x@icloud.com").login("x")
        # iCloud a mitad de copia: se detiene ordenadamente y conserva lo parcial
        fake = icloud_mod.ICloudBackend.__new__(icloud_mod.ICloudBackend)
        fake._assets, fake._sizes = {}, {}
        it = MediaScanner(phone).scan().local_items[0]
        it.source = "icloud"
        m = Manifest(tmp_path / "dest")
        rep = TransferEngine(fake, m, TransferOptions(limit=1)).run(build_plan([it], TransferOptions(limit=1), m))
        assert rep.interrupted and "DATOS" in rep.interrupted and rep.copied_files == 0
        # el cable no usa internet: copia normal aunque haya hotspot
        res = scan(phone)
        rep2, _ = copy_all(phone, res.items, Manifest(tmp_path / "dest2"))
        assert not rep2.failed and rep2.items_done > 0
        # si el usuario lo permite explícitamente, iCloud ya no se bloquea por esto
        no_network.allow_phone_data(True)
        assert not no_network._phone_data_blocked()
    finally:
        no_network.allow_phone_data(False)
        no_network.allow_icloud(False)


def test_windows_compatible_copies(phone, tmp_path):
    """HEIC → JPG (con EXIF) y video HEVC → MP4 H.264, sin tocar los originales."""
    import subprocess
    from PIL import Image
    from iphone_vault.transfer.compat import COMPAT_DIR, ffmpeg_exe
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    rep, _ = copy_all(phone, res.items, m, compat_photos=True, compat_videos=True)
    assert rep.compat_created >= 5 and not rep.compat_failed
    jpgs = list((m.root / COMPAT_DIR).rglob("*.jpg"))
    assert jpgs
    with Image.open(jpgs[0]) as im:
        assert im.format == "JPEG" and dict(im.getexif()).get(0x010F) == "Apple"
    # los originales siguen intactos y registrados
    n_orig = sum(1 for p in m.root.rglob("*.HEIC") if COMPAT_DIR not in p.parts)
    assert n_orig == len(jpgs)
    # video HEVC real (si este ffmpeg puede crear uno)
    exe = ffmpeg_exe()
    src = m.root / "2026" / "2026-01" / "VID_HEVC.MOV"
    src.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([exe, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "1",
                        "-c:v", "libx265", "-tag:v", "hvc1", str(src)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("este ffmpeg no tiene libx265 para crear el video de prueba")
    from iphone_vault.transfer.compat import make_compatible
    out = make_compatible(m.root, "2026/2026-01/VID_HEVC.MOV")
    info = subprocess.run([exe, "-hide_banner", "-i", str(m.root / out)], capture_output=True, text=True).stderr
    assert "Video: h264" in info


def test_reorganize_to_year_and_remove_json(phone, tmp_path):
    """Lo copiado con la organización vieja (año/año-mes + .json) se pasa a «una carpeta por año»."""
    from iphone_vault.config import LAYOUT_BY_DATE, LAYOUT_YEAR
    from iphone_vault.organizer.reorganize import count_pending, count_sidecars, reorganize
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    old = TransferOptions(limit=0, layout=LAYOUT_BY_DATE, write_sidecar_json=True)
    TransferEngine(phone, m, old).run(build_plan(res.items, old, m))
    before = {r["remote_path"]: r["sha256"] for r in m.all_files()}
    assert count_pending(m, LAYOUT_YEAR) == len(before) and count_sidecars(m) > 0
    rep = reorganize(m, LAYOUT_YEAR, True, [], lambda *a: None)
    assert not rep.failed and rep.moved == len(before) and rep.sidecars_removed > 0
    assert count_pending(m, LAYOUT_YEAR) == 0 and count_sidecars(m) == 0
    assert not list(m.root.rglob("*.json"))
    for r in m.all_files():
        p = m.root / r["local_path"]
        assert len(p.relative_to(m.root).parts) == 2 and p.parent.name.isdigit()     # «2025/IMG_….HEIC»
        assert hashlib.sha256(p.read_bytes()).hexdigest() == before[r["remote_path"]]
    # nada se vuelve a copiar después de reordenar
    assert not build_plan(scan(phone).items, TransferOptions(limit=0, layout=LAYOUT_YEAR), m).items
    # las carpetas viejas de mes quedaron borradas (estaban vacías)
    assert not [d for d in m.root.rglob("*") if d.is_dir() and "-" in d.name and ".vault" not in d.parts]


def test_capture_date_is_file_date(phone, tmp_path):
    m = Manifest(tmp_path / "dest")
    res = scan(phone)
    copy_all(phone, res.items, m)
    it = next(i for i in res.items if i.name == "IMG_0010.HEIC")
    p = m.root / m.file_record(it.files[0].path)["local_path"]
    assert p.parent.name == "2025"
    assert abs(p.stat().st_mtime - it.capture_date.timestamp()) < 2


def test_icloud_expired_links_are_renewed(tmp_path):
    """Enlaces vencidos (410 como excepción de pyicloud) + «${f}» en la URL: se renuevan y se completa."""
    from datetime import datetime, timezone
    from iphone_vault.device.icloud import ICloudBackend
    blobs = {f"A{i}": os.urandom(200_000 + i) for i in range(3)}
    st = {"lists": 0, "urls": []}

    class Gone(Exception):
        def __init__(self):
            super().__init__("Apple no longer serves cvws-h2.icloud-content.com/B/x/${f}")
            self.response = type("R", (), {"status_code": 410})()

    class Res:
        def __init__(self, aid):
            self.url = f"https://cvws-h2.icloud-content.com/B/{aid}-gen{st['lists']}/${{f}}?o=1"
            self.size, self.filename = len(blobs[aid]), f"IMG_{aid}.HEIC"

    class Asset:
        def __init__(self, aid):
            self.id, self.filename, self.item_type, self.size = aid, f"IMG_{aid}.HEIC", "image", len(blobs[aid])
            self.created = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.resources = {"original": Res(aid)}

        def download_url(self, v):
            return self.resources[v].url

    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.body

        def close(self):
            pass

    class Session:
        def get(self, url, stream, headers, timeout):
            st["urls"].append(url)
            if "gen1" in url:          # los enlaces del primer listado ya vencieron
                raise Gone()
            return Resp(blobs[url.split("/B/")[1].split("-")[0]])

    class Photos:
        session = Session()

        @property
        def all(self):
            st["lists"] += 1
            return [Asset(a) for a in blobs]

    class API:
        photos = Photos()

        def authenticate(self, force_refresh=False):
            pass

    class Acc:
        apple_id, api, is_demo = "x@icloud.com", API(), False

    b = ICloudBackend(Acc())
    b.is_present = lambda: True
    items = b.list_items()
    m = Manifest(tmp_path / "dest")
    opt = TransferOptions(limit=0, verify_structure=False)
    rep = TransferEngine(b, m, opt).run(build_plan(items, opt, m))
    assert not rep.failed and rep.items_done == 3 and rep.reconnections >= 1
    assert all("${f}" not in u and "IMG_A" in u for u in st["urls"])
