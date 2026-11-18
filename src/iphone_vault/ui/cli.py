"""Línea de comandos: diagnóstico y transferencia sin interfaz gráfica.

  python -m iphone_vault                     → abre la interfaz gráfica
  python -m iphone_vault diagnose [--demo] [--source android|iphone|carpeta] [--folder E:\\]
                                            → informe técnico (sin datos personales) para depurar
  python -m iphone_vault scan --dest D:\\Fotos [--demo] [--source …]
  python -m iphone_vault transfer --dest D:\\Fotos [--limit 10] [--pending] [--demo] [--source …]

--source: «auto» (por defecto: lo que esté enchufado), «iphone», «android», «android-adb», «android-mtp»,
«carpeta» (con --folder: tarjeta SD, cámara, pendrive).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import __version__
from ..catalog.manifest import Manifest
from ..config import TransferOptions
from ..models import human_size
from ..scanner.generic_scanner import scan_backend
from ..security import no_network
from ..transfer.engine import TransferEngine, build_plan, register_unavailable


def _backend(args):
    src = getattr(args, "source", "auto") or "auto"
    if args.demo:
        if src.startswith("android"):
            from ..device.android_demo import DemoAndroidBackend
            return DemoAndroidBackend(slow=False)
        from ..device.demo import DemoBackend
        return DemoBackend(slow=False)
    if src == "carpeta":
        if not getattr(args, "folder", None):
            raise SystemExit("Falta --folder (la tarjeta SD, cámara o carpeta).")
        from ..device.folder import FolderBackend
        return FolderBackend(args.folder)
    if src == "iphone":
        from ..device.iphone import IPhoneBackend
        return IPhoneBackend()
    if src == "android-adb":
        from ..device.adb import AdbBackend
        return AdbBackend()
    if src == "android-mtp":
        from ..device.mtp_windows import MtpWindowsBackend
        return MtpWindowsBackend()
    from ..device.detect import detect_devices
    found = [f for f in detect_devices() if f.ready and (src == "auto" or f.kind in ("adb", "mtp"))]
    if not found:
        raise SystemExit("No encuentro ningún celular conectado por cable.")
    print("Usando:", found[0].label, "-", found[0].detail)
    return found[0].make()


def _diagnose_android() -> dict:
    """Pasos de diagnóstico de Android, sin datos personales (solo sí/no y cantidades)."""
    import platform
    rep: dict = {"so": platform.platform()}
    try:
        from ..device.adb import find_adb, list_devices
        exe = find_adb()
        rep["adb_instalado"] = bool(exe)
        if exe:
            devs = list_devices(exe)
            rep["adb_aparatos_usb"] = [{"estado": d.state, "modelo": d.model} for d in devs]
    except Exception as e:  # noqa: BLE001
        rep["adb_error"] = type(e).__name__
    if sys.platform == "win32":
        try:
            from ..device import mtp_windows as mtp
            rep["comtypes"] = mtp.available()
            devs = mtp.list_devices()
            rep["mtp_aparatos"] = [{"nombre": d["name"], "fabricante": d["manufacturer"]} for d in devs]
            if len(devs) == 1:
                b = mtp.MtpWindowsBackend()
                info = b.connect()
                rep["mtp_conectado"] = {"modelo": info.model, "almacenamientos": sorted(b._storages)}  # noqa: SLF001
                for st in b.listdir(b.ns):
                    base = f"{b.ns}/{st}"
                    rep.setdefault("mtp_carpetas", {})[st] = [n for n in b.listdir(base)
                                                              if n in ("DCIM", "Pictures", "Movies", "Android",
                                                                       "WhatsApp", "Download", "Telegram")]
                b.close()
        except Exception as e:  # noqa: BLE001
            rep["mtp_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return rep


def cmd_diagnose(args) -> int:
    android_steps = _diagnose_android() if (args.source or "auto").startswith("android") and not args.demo \
        else None
    b = _backend(args)
    info = b.connect(on_status=print)
    print(f"Conectado: {info.label} · {info.connection}")
    res = scan_backend(b, progress=lambda m, d, t: print("  ", m) if m else None, options=TransferOptions())
    report = {
        "version": __version__,
        "platform": info.platform,
        "os_version": info.ios_version,
        "connection": info.connection,
        "model": info.model,
        "android_steps": android_steps,
        "photos_db_available": res.photos_db_available,
        "stats": res.stats,
        "warnings": res.warnings,
        "network_blocked_attempts": len(no_network.blocked_attempts),
    }
    out = Path(args.out or "diagnostico_cloudy.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"\nInforme guardado en {out.resolve()} (contiene solo cantidades, no nombres ni fotos).")
    b.close()
    return 0


def cmd_scan(args) -> int:
    b = _backend(args)
    b.connect(on_status=print)
    res = scan_backend(b, options=TransferOptions())
    m = Manifest(Path(args.dest))
    n = register_unavailable(res.items, m)
    plan = build_plan(res.items, TransferOptions(limit=0), m)
    print(f"Elementos: {len(res.items)} · disponibles: {len(res.local_items)} · pendientes (iCloud): {n}")
    print(f"Por copiar con opciones por defecto: {len(plan.items)} elementos, {human_size(plan.total_bytes)}")
    for w in res.warnings:
        print("AVISO:", w)
    b.close()
    return 0


def cmd_transfer(args) -> int:
    b = _backend(args)
    b.connect(on_status=print)
    res = scan_backend(b, options=TransferOptions())
    m = Manifest(Path(args.dest))
    register_unavailable(res.items, m)
    opt = TransferOptions(limit=args.limit)
    plan = build_plan(res.items, opt, m, only_pending=args.pending)
    print(f"Copiando {len(plan.items)} elementos ({plan.file_count} archivos, {human_size(plan.total_bytes)})…")
    rep = TransferEngine(b, m, opt, log_cb=print).run(plan)
    print(f"Listo: {rep.copied_files} archivos copiados y verificados, {len(rep.failed)} errores, "
          f"{rep.seconds:.1f} s")
    b.close()
    return 1 if rep.failed else 0


def main(argv=None) -> int:
    no_network.install()
    from ..security.cleanup_tmp import purge_stale
    from ..security.logs import get_logger
    get_logger()
    purge_stale()   # restos de una ejecución que se cortó de golpe (incluye copias de Photos.sqlite)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="iphone_vault")
    sub = p.add_subparsers(dest="cmd")
    sources = ["auto", "iphone", "android", "android-adb", "android-mtp", "carpeta"]
    d = sub.add_parser("diagnose")
    d.add_argument("--demo", action="store_true")
    d.add_argument("--out")
    s = sub.add_parser("scan")
    s.add_argument("--dest", required=True)
    s.add_argument("--demo", action="store_true")
    for x in (d, s):
        x.add_argument("--source", choices=sources, default="auto")
        x.add_argument("--folder")
    t = sub.add_parser("transfer")
    t.add_argument("--source", choices=sources, default="auto")
    t.add_argument("--folder")
    t.add_argument("--dest", required=True)
    t.add_argument("--limit", type=int, default=10)
    t.add_argument("--pending", action="store_true", help="solo los elementos de la lista de pendientes")
    t.add_argument("--demo", action="store_true")
    args = p.parse_args(argv)
    if args.cmd is None:
        from .gui.main_window import run_gui
        return run_gui()
    return {"diagnose": cmd_diagnose, "scan": cmd_scan, "transfer": cmd_transfer}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
