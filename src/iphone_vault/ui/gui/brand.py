"""Logo de Cloudy (el mismo de los archivos de marca), dibujado desde SVG: una nube que se apoya sobre un disco."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512"><defs><linearGradient id="cg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5a96ff"/><stop offset="0.5" stop-color="#2f6bff"/><stop offset="1" stop-color="#6a45f5"/></linearGradient><linearGradient id="hl" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".20"/><stop offset=".45" stop-color="#fff" stop-opacity="0"/></linearGradient></defs><rect width="512" height="512" rx="116" fill="url(#cg)"/><rect width="512" height="512" rx="116" fill="url(#hl)"/><g transform="translate(0 -6) scale(1.0)"><clipPath id="cc477485"><rect x="0" y="0" width="512" height="312"/></clipPath><g clip-path="url(#cc477485)"><circle cx="172" cy="252" r="64" fill="#ffffff"/><circle cx="262" cy="206" r="92" fill="#ffffff"/><circle cx="344" cy="250" r="60" fill="#ffffff"/><path d="M108 251 H404 V281 A31 31 0 0 1 373 312 H139 A31 31 0 0 1 108 281 Z" fill="#ffffff"/></g><rect x="108" y="330" width="296" height="62" rx="24" fill="#ffffff"/><circle cx="370" cy="361" r="11" fill="#3ddc84"/><rect x="138" y="355" width="128" height="12" rx="6" fill="url(#cg)" opacity=".35"/><path d="M256 168 V268 M214 228 L256 270 L298 228" fill="none" stroke="url(#cg)" stroke-width="30" stroke-linecap="round" stroke-linejoin="round"/></g></svg>'


@lru_cache(maxsize=16)
def logo_pixmap(size: int = 64) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(ICON_SVG.encode())).render(p, QRectF(0, 0, size * ratio, size * ratio))
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(logo_pixmap(s))
    return ic
