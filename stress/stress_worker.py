"""Proceso aparte para pruebas que matan el proceso (kill -9) o miden fugas de archivos."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from synthetic import Faults, SyntheticBackend, SyntheticLibrary  # noqa: E402

from iphone_vault.catalog.manifest import Manifest  # noqa: E402
from iphone_vault.config import TransferOptions  # noqa: E402
from iphone_vault.scanner.media_scanner import MediaScanner  # noqa: E402
from iphone_vault.security import no_network  # noqa: E402
from iphone_vault.transfer import engine as engine_mod  # noqa: E402
from iphone_vault.transfer.engine import TransferEngine, build_plan  # noqa: E402

engine_mod.TransferEngine._make_compatible_copies = lambda self: None


def main(dest, n, throughput, latency):
    no_network.install()
    try:
        from iphone_vault.security.cleanup_tmp import purge_stale
        purge_stale()
    except ImportError:
        pass
    lib = SyntheticLibrary(n, seed=11, fixed_size=12 * 1024 * 1024)
    b = SyntheticBackend(lib, Faults(throughput_mbps=throughput, latency_s=latency))
    b.connect()
    m = Manifest(Path(dest))
    res = MediaScanner(b).scan()
    opt = TransferOptions(limit=0)
    plan = build_plan(res.items, opt, m)
    rep = TransferEngine(b, m, opt).run(plan)
    print(json.dumps({"copied": rep.copied_files, "resumed": rep.resumed_files, "failed": len(rep.failed),
                      "planned_total": len(m.all_files())}))


def main_slow_scan(dest):
    lib = SyntheticLibrary(2000, seed=11, fixed_size=1024)

    class Slow(SyntheticBackend):
        def read_chunks(self, path, chunk_size=4 * 1024 * 1024, offset=0):
            if path == "PhotoData/Photos.sqlite":
                data = self.lib._db[offset:]
                for i in range(0, len(data), 16 * 1024):
                    time.sleep(0.3)
                    yield data[i:i + 16 * 1024]
                return
            yield from super().read_chunks(path, chunk_size, offset)

    b = Slow(lib)
    b.connect()
    MediaScanner(b).scan()
