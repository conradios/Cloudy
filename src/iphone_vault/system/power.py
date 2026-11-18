"""Evita que la compu se suspenda mientras se copia o se verifica (y no apaga la pantalla a propósito).

- Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED). Vale para el hilo que lo
  pide, así que se usa DENTRO del hilo de trabajo, y se libera al terminar.
- macOS: el comando del sistema `caffeinate -i` mientras dura la tarea.
- Linux: sin efecto.
"""
from __future__ import annotations

import os
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self):
        self._proc = None
        self.active = False

    def __enter__(self):
        try:
            if os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self.active = True
            elif sys.platform == "darwin":
                self._proc = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])  # nosec B603
                self.active = True
        except Exception:  # noqa: BLE001
            self.active = False
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt" and self.active:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            if self._proc is not None:
                self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return False
