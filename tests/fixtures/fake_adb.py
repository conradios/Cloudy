#!/usr/bin/env python3
"""ADB falso para las pruebas: emula un celular cuyo almacenamiento es una carpeta de la compu.

Variables: FAKE_ADB_ROOT (carpeta con «sdcard/» y «storage/»), FAKE_ADB_STATE (device|unauthorized),
FAKE_ADB_BAD_SHA=1 (sha256sum devuelve una huella falsa), FAKE_ADB_LOG (registra cada comando).
Los comandos del «celular» se ejecutan de verdad con sh, así se prueba el escapado de rutas.
"""
import os
import subprocess
import sys

root = os.environ["FAKE_ADB_ROOT"]
args = sys.argv[1:]
if os.environ.get("FAKE_ADB_LOG"):
    with open(os.environ["FAKE_ADB_LOG"], "a", encoding="utf-8") as fh:
        fh.write(repr(args) + "\n")
if args[:2] == ["devices", "-l"]:
    state = os.environ.get("FAKE_ADB_STATE", "device")
    print("List of devices attached")
    print(f"R5CT123ABC     {state} usb:1-1 product:e1qxxx model:SM_S921B device:e1q transport_id:1")
    print("192.168.0.10:5555 device product:x model:WIFI_PHONE device:x transport_id:2")
    print("emulator-5554  device product:sdk model:Emu device:x transport_id:3")
    sys.exit(0)
if args[:2] == ["-s", "R5CT123ABC"]:
    args = args[2:]
else:
    sys.stderr.write("error: more than one device/emulator\n")
    sys.exit(1)
if os.environ.get("FAKE_ADB_STATE") == "unauthorized":
    sys.stderr.write("error: device unauthorized.\n")
    sys.exit(1)
if args[0] != "exec-out":
    sys.exit(2)
cmd = args[1]
if cmd == "getprop":
    sys.stdout.write("[ro.product.brand]: [samsung]\n[ro.product.model]: [SM-S921B]\n"
                     "[ro.serialno]: [R5CT123ABC]\n[ro.build.version.release]: [15]\n"
                     "[ro.product.marketname]: [Galaxy S24]\n")
    sys.exit(0)
if cmd.startswith("sha256sum") and os.environ.get("FAKE_ADB_BAD_SHA"):
    print("0" * 64 + "  x")
    sys.exit(0)
sd, st = os.path.join(root, "sdcard"), os.path.join(root, "storage")
real = cmd.replace("/sdcard", sd).replace("/storage", st)
out = subprocess.run(["sh", "-c", real], capture_output=True).stdout
out = out.replace(sd.encode(), b"/sdcard").replace(st.encode(), b"/storage")
sys.stdout.buffer.write(out)
