"""v0.5: verificación de integridad, reparación, respaldo del catálogo, registros sin datos personales, disco."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iphone_vault.catalog.backup import is_healthy, list_backups  # noqa: E402
from iphone_vault.catalog.manifest import STATUS_FAILED, Manifest  # noqa: E402
from iphone_vault.config import TransferOptions  # noqa: E402
from iphone_vault.device.demo import DemoBackend, build_demo_library  # noqa: E402
from iphone_vault.scanner.media_scanner import MediaScanner  # noqa: E402
from iphone_vault.security.logs import mask  # noqa: E402
from iphone_vault.system.disk import FAT_LIMIT, disk_info, too_big_for_fat  # noqa: E402
from iphone_vault.system.power import KeepAwake  # noqa: E402
from iphone_vault.transfer.engine import TransferEngine, build_plan  # noqa: E402
from iphone_vault.transfer.verify import last_verification, repair, verify_library  # noqa: E402


def _copied(tmp_path, n=6):
    root = build_demo_library(tmp_path / "iphone")
    phone = DemoBackend(root, slow=False)
    phone.connect()
    items = MediaScanner(phone).scan().items
    m = Manifest(tmp_path / "dest")
    opt = TransferOptions(limit=n)
    rep = TransferEngine(phone, m, opt).run(build_plan(items, opt, m))
    assert not rep.failed
    return phone, items, m


def test_verify_healthy_then_detects_damage_and_repair_recopies(tmp_path):
    phone, items, m = _copied(tmp_path)
    rep = verify_library(m, lambda *a: None)
    assert rep.healthy and rep.ok == rep.checked == len(m.all_files())
    assert last_verification(m)["problems"] == 0 and last_verification(m)["at"] is not None

    files = m.all_files()
    bad = m.root / files[0]["local_path"]
    data = bytearray(bad.read_bytes())
    data[len(data) // 2] ^= 0xFF                       # un bit dañado («bit rot»), mismo tamaño
    bad.write_bytes(bytes(data))
    (m.root / files[1]["local_path"]).unlink()         # uno borrado a mano
    rep = verify_library(m, lambda *a: None)
    kinds = sorted(p.kind for p in rep.problems)
    assert kinds == ["dañado", "falta"]
    assert last_verification(m)["problems"] == 2

    assert repair(m, rep.problems) == 2
    assert Path(str(bad) + ".danado").exists()          # el dañado no se borra
    rec = m.file_record(files[0]["remote_path"])
    assert rec is None and m.item_status(files[0]["item_key"]) == STATUS_FAILED
    plan = build_plan(items, TransferOptions(limit=0), m)
    keys = {it.key for it in plan.items}
    assert files[0]["item_key"] in keys and files[1]["item_key"] in keys
    TransferEngine(phone, m, TransferOptions(limit=0)).run(plan)
    assert verify_library(m, lambda *a: None).healthy


def test_verify_reports_leftover_parts_and_can_cancel(tmp_path):
    import threading
    _phone, _items, m = _copied(tmp_path, 3)
    (m.root / "2020").mkdir(exist_ok=True)
    (m.root / "2020" / "IMG_9.HEIC.part").write_bytes(b"x")
    rep = verify_library(m, lambda *a: None)
    assert rep.leftovers and rep.healthy
    ev = threading.Event()
    ev.set()
    assert verify_library(m, lambda *a: None, ev).cancelled


def test_catalog_backup_and_auto_restore(tmp_path):
    _phone, _items, m = _copied(tmp_path, 3)
    n_files = len(m.all_files())
    assert list_backups(m.dir)                           # la copia respalda sola al terminar
    db = m.path
    m.close()
    with open(db, "r+b") as fh:                         # dañar el catálogo
        fh.seek(0)
        fh.write(b"\x00" * 4096)
    for side in ("-wal", "-shm"):
        p = Path(str(db) + side)
        if p.exists():
            p.unlink()
    assert not is_healthy(db)
    m2 = Manifest(tmp_path / "dest")
    assert m2.restored_from is not None
    assert len(m2.all_files()) == n_files
    assert list(m2.dir.glob("manifest-danado-*.db"))     # el dañado se guarda aparte
    m2.close()


def test_backups_keep_only_last_7(tmp_path):
    m = Manifest(tmp_path / "d")
    import time
    for i in range(9):
        p = m.backup()
        p.rename(p.with_name(f"manifest-2026010{i}-000000.db"))
        time.sleep(0.01)
    m.backup()
    assert len(list_backups(m.dir)) <= 8
    con = sqlite3.connect(str(list_backups(m.dir)[0]))
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    con.close()
    m.close()


def test_logs_mask_personal_data():
    out = mask("Sesión de iCloud iniciada: juan.perez@icloud.com · código 482913 · IMG_482913.HEIC · "
               "token Ab3d" + "x" * 50)
    assert "juan.perez" not in out and "j***@icloud.com" in out
    assert "482913 " not in out.replace("IMG_482913", "")
    assert "IMG_482913.HEIC" in out
    assert "x" * 50 not in out


def test_disk_info_and_fat_limit(tmp_path):
    di = disk_info(tmp_path)
    assert di and di.total > 0 and di.free > 0
    assert disk_info(tmp_path / "no-existe") is None
    assert too_big_for_fat([1, FAT_LIMIT, FAT_LIMIT + 1, 10 * FAT_LIMIT]) == 2


def test_keep_awake_is_safe_everywhere():
    with KeepAwake() as k:
        assert k is not None


def test_fat32_destination_skips_files_over_4gb(tmp_path, monkeypatch):
    import iphone_vault.system.disk as disk
    from iphone_vault.system.disk import DiskInfo
    root = build_demo_library(tmp_path / "iphone")
    phone = DemoBackend(root, slow=False)
    phone.connect()
    items = MediaScanner(phone).scan().items
    m = Manifest(tmp_path / "dest")
    opt = TransferOptions(limit=5)
    plan = build_plan(items, opt, m)
    biggest = max(f.size for _it, fs in plan.entries for f in fs)
    monkeypatch.setattr(disk, "FAT_LIMIT", biggest - 1)
    monkeypatch.setattr(disk, "disk_info", lambda p: DiskInfo(str(p), "vfat", 10**12, 10**11))
    rep = TransferEngine(phone, m, opt).run(plan)
    assert rep.failed and all("FAT32" in why for _p, why in rep.failed)
    assert rep.items_done == 5 - len({p for p, _ in rep.failed}) or rep.items_done < 5
    assert any("FAT32" in w for w in rep.warnings)
