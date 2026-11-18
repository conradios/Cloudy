"""Android (Samsung y demás), tarjetas SD, cámaras y carpetas: escaneo, copia verificada y seguridad."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iphone_vault.catalog.manifest import Manifest  # noqa: E402
from iphone_vault.config import TransferOptions  # noqa: E402
from iphone_vault.device import adb as adbmod  # noqa: E402
from iphone_vault.device.adb import AdbBackend, AdbUnauthorized, parse_devices, sh_quote  # noqa: E402
from iphone_vault.device.android_common import safe_serial, storage_slug  # noqa: E402
from iphone_vault.device.android_demo import DemoAndroidBackend  # noqa: E402
from iphone_vault.device.folder import FolderBackend  # noqa: E402
from iphone_vault.models import ScanResult  # noqa: E402
from iphone_vault.organizer.dates import date_from_filename  # noqa: E402
from iphone_vault.organizer.layout import item_folder  # noqa: E402
from iphone_vault.scanner.generic_scanner import scan_backend  # noqa: E402
from iphone_vault.transfer.engine import TransferEngine, build_plan  # noqa: E402

FAKE_ADB = Path(__file__).parent / "fixtures" / "fake_adb.py"


def _run(backend, dest, opt=None) -> tuple:
    opt = opt or TransferOptions(limit=0)
    res: ScanResult = scan_backend(backend, options=opt)
    m = Manifest(dest)
    rep = TransferEngine(backend, m, opt).run(build_plan(res.items, opt, m))
    return res, m, rep


# ------------------------------------------------------------------ fechas
@pytest.mark.parametrize("name,expected", [
    ("20240115_143012.jpg", (2024, 1, 15, 14, 30, 12)),
    ("PXL_20240115_143012345.MP.jpg", (2024, 1, 15, 14, 30, 12)),
    ("IMG_20240115_143012.jpg", (2024, 1, 15, 14, 30, 12)),
    ("VID_20240115_143012.mp4", (2024, 1, 15, 14, 30, 12)),
    ("Screenshot_20240115-143012_WhatsApp.jpg", (2024, 1, 15, 14, 30, 12)),
    ("Screenshot_2024-01-15-14-30-12-123_com.app.png", (2024, 1, 15, 14, 30, 12)),
    ("IMG-20240115-WA0001.jpg", (2024, 1, 15, 12, 0, 0)),
    ("IMG_0001.HEIC", None), ("DSC01234.ARW", None), ("20241345_999999.jpg", None),
])
def test_date_from_filename(name, expected):
    d = date_from_filename(name)
    assert (d is None) if expected is None else (d.timetuple()[:6] == expected)


def test_storage_and_serial_naming():
    assert storage_slug("Almacenamiento interno", 0) == "interno"
    assert storage_slug("Internal shared storage", 1, False) == "interno"
    assert storage_slug("Tarjeta SD", 1, True, "1A2B-3C4D") == "sd-1A2B3C4D"
    assert storage_slug("Card", 1) == "sd-1"
    assert safe_serial("../../etc/x y") == "etc_x_y"
    assert "/" not in safe_serial("a/b") and safe_serial("") == "sin-serie"


# ------------------------------------------------------------------ Samsung simulado
def test_android_demo_end_to_end(tmp_path):
    phone = DemoAndroidBackend(tmp_path / "phone", slow=False)
    info = phone.connect()
    assert info.platform == "Android" and info.kind == "android"
    res, m, rep = _run(phone, tmp_path / "dest")
    names = {i.name for i in res.items}
    # no se copian papelera, miniaturas, enviadas de WhatsApp, descargas (por defecto) ni documentos
    assert not any(n.startswith(".trashed") for n in names)
    assert "1234567.jpg" not in names and "IMG-20250303-WA0002.jpg" not in names
    assert "meme.png" not in names and "contrato.pdf" not in names
    assert {"IMG-20250303-WA0001.jpg", "VID-20250621-WA0003.mp4", "554433_121.jpg",
            "20260909_131300.jpg"} <= names
    assert not rep.failed and rep.copied_files == len(res.items) == 13
    # todo quedó por año, con el nombre original, y el catálogo usa rutas con el número de serie
    dest = tmp_path / "dest"
    assert (dest / "2025" / "IMG-20250303-WA0001.jpg").is_file()
    assert (dest / "2026" / "20260909_131300.jpg").is_file()
    assert all(f["remote_path"].startswith("android/DEMO-GALAXY-S24/") for f in m.all_files())
    weird = [p for p in dest.rglob("*comillas*")]
    assert len(weird) == 1 and weird[0].parent.name == "2025"
    # segunda pasada: nada nuevo
    res2, m2, rep2 = _run(phone, dest)
    assert rep2.copied_files == 0
    # el celular quedó intacto
    assert (tmp_path / "phone" / "interno" / "DCIM" / "Camera" / "20230314_102100.jpg").is_file()


def test_android_demo_options_messaging_downloads(tmp_path):
    phone = DemoAndroidBackend(tmp_path / "phone", slow=False)
    phone.connect()
    res = scan_backend(phone, options=TransferOptions(android_messaging=False, android_downloads=True))
    names = {i.name for i in res.items}
    assert "meme.png" in names and not any("WA" in n for n in names) and "554433_121.jpg" not in names


def test_android_demo_cable_cut_resumes(tmp_path):
    phone = DemoAndroidBackend(tmp_path / "phone", slow=False)
    phone.connect()
    phone.simulate_unplug(after_bytes=1_500_000, replug_after_seconds=0.5)
    res, m, rep = _run(phone, tmp_path / "dest")
    assert not rep.failed and rep.reconnections >= 1 and rep.copied_files == len(res.items)


def test_mirror_layout_sanitizes_device_folders(tmp_path):
    phone = DemoAndroidBackend(tmp_path / "phone", slow=False)
    phone.connect()
    item = scan_backend(phone, options=TransferOptions()).items[0]
    item.primary_path = "android/X/interno/../../CON/a:b/x.jpg"
    f = item_folder(item, "mirror")
    assert ".." not in f.split("/") and ":" not in f and "CON" not in f.split("/")


# ------------------------------------------------------------------ carpeta / tarjeta SD / cámara
def _card(tmp_path):
    card = tmp_path / "card"
    (card / "DCIM" / "100MSDCF").mkdir(parents=True)
    (card / "DCIM" / "100MSDCF" / "DSC01234.JPG").write_bytes(b"\xff\xd8\xff" + os.urandom(5000) + b"\xff\xd9")
    (card / "DCIM" / "100MSDCF" / "DSC01234.ARW").write_bytes(os.urandom(8000))
    (card / "PRIVATE" / "M4ROOT" / "CLIP").mkdir(parents=True)
    (card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4").write_bytes(os.urandom(9000))
    (card / ".Spotlight-V100").mkdir()
    (card / ".Spotlight-V100" / "x.jpg").write_bytes(b"x" * 10)
    return card


def test_folder_source_copies_camera_card(tmp_path):
    card = _card(tmp_path)
    b = FolderBackend(card)
    assert b.connect().kind == "folder"
    res, m, rep = _run(b, tmp_path / "dest")
    assert sorted(i.name for i in res.items) == ["C0001.MP4", "DSC01234.ARW", "DSC01234.JPG"]
    assert not rep.failed and rep.copied_files == 3


def test_folder_source_never_reads_destination_or_outside(tmp_path):
    card = _card(tmp_path)
    b = FolderBackend(card)
    b.connect()
    _run(b, card / "Cloudy")                          # destino DENTRO del origen (mala idea)
    b2 = FolderBackend(card)
    b2.connect()
    res2 = scan_backend(b2, options=TransferOptions())
    assert not any("/Cloudy/" in i.primary_path for i in res2.items)   # no se copia lo ya copiado
    outside = tmp_path / "secreto.jpg"
    outside.write_bytes(b"no")
    try:
        (card / "DCIM" / "link.jpg").symlink_to(outside)
        (card / "DCIM" / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("sin enlaces simbólicos")
    names = {i.name for i in scan_backend(b2, options=TransferOptions()).items}
    assert "link.jpg" not in names and "secreto.jpg" not in names
    for bad in (f"{b2.ns}/../secreto.jpg", f"{b2.ns}/DCIM/link.jpg", "carpeta/otro/x.jpg", "/etc/passwd"):
        with pytest.raises((PermissionError, FileNotFoundError)):
            list(b2.read_chunks(bad))


# ------------------------------------------------------------------ ADB (con un adb falso que ejecuta de verdad)
@pytest.fixture
def fake_phone(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("el adb falso usa sh")
    root = tmp_path / "fakephone"
    cam = root / "sdcard" / "DCIM" / "Camera"
    cam.mkdir(parents=True)
    (cam / "20240115_143012.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(300_000) + b"\xff\xd9")
    (cam / "20240116_101010.mp4").write_bytes(os.urandom(200_000))
    evil = root / "sdcard" / "Pictures" / "x"
    evil.mkdir(parents=True)
    for n in ("a'; touch PWNED1; '.jpg", "$(touch PWNED2).jpg", "`touch PWNED3`.jpg", "con\nsalto.jpg",
              "-rf.jpg", "ñandú — café.jpg"):
        (evil / n).write_bytes(b"\xff\xd8\xff" + os.urandom(1000) + b"\xff\xd9")
    sd = root / "storage" / "1A2B-3C4D" / "DCIM" / "Camera"
    sd.mkdir(parents=True)
    (sd / "20250101_000001.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(2000) + b"\xff\xd9")
    (root / "storage" / "emulated").mkdir()
    (root / "storage" / "self").mkdir()
    monkeypatch.setenv("FAKE_ADB_ROOT", str(root))
    monkeypatch.setenv("FAKE_ADB_LOG", str(tmp_path / "adb.log"))
    monkeypatch.chdir(tmp_path)
    return root


def test_adb_devices_only_usb():
    devs = parse_devices("List of devices attached\n"
                         "R5CT123ABC device usb:1-1 model:SM_S921B\n"
                         "192.168.0.10:5555 device model:X\n"
                         "adb-R5CT-abc._adb-tls-connect._tcp device model:Y\n"
                         "emulator-5554 device model:Emu\n"
                         "ZZZ unauthorized usb:2-1\n")
    usb = [d for d in devs if d.usb]
    assert [d.serial for d in usb] == ["R5CT123ABC", "ZZZ"] and usb[0].model == "SM S921B"


def test_sh_quote_is_safe(tmp_path):
    import subprocess
    for s in ["a'b", "$(touch X)", "`touch X`", "a;b|c&d", "x\ny", "-rf", "ñ"]:
        out = subprocess.run(["sh", "-c", f"printf %s {sh_quote(s)}"], capture_output=True, cwd=tmp_path).stdout
        assert out.decode() == s
    assert not (tmp_path / "X").exists()
    with pytest.raises(PermissionError):
        sh_quote("a\x00b")


def test_adb_end_to_end_with_device_side_sha(fake_phone, tmp_path):
    b = AdbBackend(adb=FAKE_ADB)
    info = b.connect()
    assert info.platform == "Android" and info.udid == "R5CT123ABC" and info.name == "Galaxy S24"
    assert sorted(b.listdir(b.ns)) == ["interno", "sd-1A2B3C4D"]
    res, m, rep = _run(b, tmp_path / "dest", TransferOptions(limit=0, verify_on_device=True))
    names = {i.name for i in res.items}
    assert {"20240115_143012.jpg", "20240116_101010.mp4", "20250101_000001.jpg", "-rf.jpg",
            "ñandú — café.jpg", "con\nsalto.jpg", "a'; touch PWNED1; '.jpg"} <= names
    assert not rep.failed, rep.failed
    assert rep.copied_files == len(res.items) == 9 and rep.device_verified == 9
    for n in ("PWNED1", "PWNED2", "PWNED3"):
        assert not list(fake_phone.rglob(n)) and not (tmp_path / n).exists(), "inyección de comandos"
    # nombres peligrosos guardados con nombre seguro
    assert not any("\n" in p.name for p in (tmp_path / "dest").rglob("*"))
    # solo lectura: ningún comando de escritura o borrado llegó al «celular»
    log = (tmp_path / "adb.log").read_text()
    for bad in ("rm ", "mv ", "push", "install", " > ", "tcpip", "connect"):
        assert bad not in log
    # retomar desde la mitad (dd skip)
    data = b"".join(b.read_chunks(f"{b.ns}/interno/DCIM/Camera/20240115_143012.jpg", offset=70_000))
    full = (fake_phone / "sdcard/DCIM/Camera/20240115_143012.jpg").read_bytes()
    assert data == full[70_000:]


def test_adb_device_side_sha_mismatch_is_caught(fake_phone, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_BAD_SHA", "1")
    b = AdbBackend(adb=FAKE_ADB)
    b.connect()
    res, m, rep = _run(b, tmp_path / "dest", TransferOptions(limit=2, verify_on_device=True))
    assert len(rep.failed) == 2 and rep.copied_files == 0
    assert not [p for p in (tmp_path / "dest").rglob("*") if p.is_file() and ".vault" not in p.parts]


def test_adb_unauthorized_and_paths(fake_phone, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_STATE", "unauthorized")
    with pytest.raises(AdbUnauthorized):
        AdbBackend(adb=FAKE_ADB).connect()
    monkeypatch.setenv("FAKE_ADB_STATE", "device")
    b = AdbBackend(adb=FAKE_ADB)
    b.connect()
    for bad in (f"{b.ns}/interno/../../etc/passwd", "android/OTRO/interno/x.jpg", "/sdcard/x.jpg"):
        with pytest.raises((PermissionError, FileNotFoundError)):
            b._remote(bad)  # noqa: SLF001
    assert b._remote(f"{b.ns}/interno/DCIM") == "/sdcard/DCIM"  # noqa: SLF001
    assert b._remote(f"{b.ns}/sd-1A2B3C4D/DCIM") == "/storage/1A2B-3C4D/DCIM"  # noqa: SLF001


def test_adb_env_is_local_only():
    env = adbmod.adb_env()
    assert env["ADB_MDNS"] == "0" and env["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"


def test_detect_without_anything(monkeypatch):
    from iphone_vault.device import detect

    class NoPhone:
        def is_present(self):
            return False
    monkeypatch.setattr(adbmod, "find_adb", lambda: None)
    assert detect.detect_devices(NoPhone()) == [] or sys.platform == "win32"
