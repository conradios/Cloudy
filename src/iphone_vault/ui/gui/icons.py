"""Íconos de línea (SVG propios, 24×24) dibujados en el color que se pida. Sin archivos externos."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

_P = {
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    "photos": '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/>'
              '<path d="M21 16l-5-5-9 9"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "broom": '<path d="M4 20l7-7"/><path d="M14 4l6 6-5 5-6-6z"/><path d="M9 9l-4 9 9-4"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "bell": '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1'
            'M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/>'
            '<circle cx="4" cy="18" r="1"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
    "warn": '<path d="M12 3l10 18H2z"/><path d="M12 10v5"/><circle cx="12" cy="18" r=".6"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.5" r=".6"/>',
    "question": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.7"/>'
                '<circle cx="12" cy="17" r=".6"/>',
    "phone": '<rect x="7" y="2" width="10" height="20" rx="2.5"/><path d="M11 18h2"/>',
    "cloud": '<path d="M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9a4.5 4.5 0 0 0 1 9z"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "disk": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 14h18"/><circle cx="17" cy="16.5" r=".6"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "copy": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 19h16"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "spark": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
    "wrench": '<path d="M14.5 6.5a4 4 0 0 0 5 5L12 19a2.1 2.1 0 0 1-3-3l7.5-7.5a4 4 0 0 1-2-2z"/>',
    "power": '<path d="M12 3v8"/><path d="M6.3 7a8 8 0 1 0 11.4 0"/>',
}


def svg(name: str, color: str, stroke: float = 1.8) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{_P[name]}</svg>')


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int = 20, stroke: float = 1.8) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    r = QSvgRenderer(QByteArray(svg(name, color, stroke).encode()))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, size * ratio, size * ratio))
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def icon(name: str, color: str, size: int = 20, active_color: str | None = None) -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    if active_color:
        ic.addPixmap(pixmap(name, active_color, size), QIcon.Normal, QIcon.On)
        ic.addPixmap(pixmap(name, active_color, size), QIcon.Active, QIcon.Off)
    return ic


NAMES = tuple(_P)
