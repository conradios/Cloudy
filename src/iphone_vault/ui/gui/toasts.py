"""Alertas flotantes («toasts»): aparecen arriba a la derecha, se apilan y se van solas.

Mismo diseño que las ventanas de aviso: ícono en círculo de color, título, texto y un botón de acción
opcional. Pasar el mouse por encima las mantiene; los errores duran más.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from . import icons, theme
from .dialogs import KINDS

DURATION = {"success": 4500, "info": 5000, "warn": 8000, "error": 12000}
WIDTH = 360
MARGIN = 18
GAP = 10
MAX_VISIBLE = 4

TOAST_QSS = f"""
QFrame#toast {{ background: {theme.CARD_2}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}
QLabel#toastTitle {{ font-weight: 700; font-size: 13.5px; }}
QLabel#toastText {{ color: #c3c8e4; font-size: 12.5px; }}
QPushButton#toastClose {{ background: transparent; border: none; padding: 2px; border-radius: 5px; }}
QPushButton#toastClose:hover {{ background: {theme.BORDER}; }}
QPushButton#toastAction {{ background: transparent; border: none; color: {theme.ACCENT_HOVER}; font-weight: 600;
                            padding: 0; text-align: left; }}
QPushButton#toastAction:hover {{ text-decoration: underline; }}
"""


class Toast(QFrame):
    def __init__(self, host: "ToastHost", kind: str, title: str, text: str,
                 action: Optional[tuple[str, Callable]] = None):
        super().__init__(host.parent_widget)
        self.host = host
        self.setObjectName("toast")
        self.setStyleSheet(TOAST_QSS)
        self.setFixedWidth(WIDTH)
        icon_name, color, bg = KINDS.get(kind, KINDS["info"])
        self.setStyleSheet(TOAST_QSS + f"QFrame#toast {{ border-left: 3px solid {color}; }}")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 11, 8, 11)
        lay.setSpacing(11)
        badge = QLabel()
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background:{bg}; border-radius:15px;")
        badge.setPixmap(icons.pixmap(icon_name, color, 16, 2.0))
        lay.addWidget(badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("toastTitle")
        t.setWordWrap(True)
        col.addWidget(t)
        if text:
            b = QLabel(text if len(text) < 260 else text[:257] + "…")
            b.setObjectName("toastText")
            b.setWordWrap(True)
            col.addWidget(b)
        if action:
            a = QPushButton(action[0])
            a.setObjectName("toastAction")
            a.setCursor(Qt.PointingHandCursor)
            a.clicked.connect(lambda: (action[1](), self.dismiss()))
            col.addWidget(a, 0, Qt.AlignLeft)
        lay.addLayout(col, 1)
        x = QPushButton()
        x.setObjectName("toastClose")
        x.setIcon(icons.icon("close", theme.MUTED, 14))
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.dismiss)
        lay.addWidget(x, 0, Qt.AlignTop)

        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(28)
        sh.setOffset(0, 6)
        sh.setColor(QColor(0, 0, 0, 150))
        self._shadow = sh
        self.setGraphicsEffect(sh)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timer.start(DURATION.get(kind, 5000))
        self._closing = False

    def enterEvent(self, e):
        self._timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._closing:
            self._timer.start(2500)
        super().leaveEvent(e)

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(180)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._remove)
        anim.start()
        self._anim = anim

    def _remove(self):
        self.host._forget(self)  # noqa: SLF001
        self.deleteLater()


class ToastHost(QObject):
    """Administra la pila de toasts sobre un widget (normalmente la ventana principal)."""

    def __init__(self, parent_widget: QWidget):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.toasts: list[Toast] = []
        parent_widget.installEventFilter(self)

    def show(self, kind: str, title: str, text: str = "", action: Optional[tuple[str, Callable]] = None) -> Toast:
        while len(self.toasts) >= MAX_VISIBLE:
            old = self.toasts.pop(0)
            old.hide()
            old.deleteLater()
        t = Toast(self, kind, title, text, action)
        self.toasts.append(t)
        t.adjustSize()
        t.show()
        t.raise_()
        self._layout()
        return t

    def _forget(self, t: Toast):
        if t in self.toasts:
            self.toasts.remove(t)
        self._layout()

    def _layout(self):
        w = self.parent_widget
        y = MARGIN
        for t in reversed(self.toasts):           # el más nuevo arriba
            t.adjustSize()
            t.move(w.width() - WIDTH - MARGIN, y)
            t.raise_()
            y += t.height() + GAP

    def eventFilter(self, obj, ev):
        if obj is self.parent_widget and ev.type() == QEvent.Resize:
            self._layout()
        return False
