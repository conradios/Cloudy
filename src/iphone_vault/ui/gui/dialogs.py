"""Ventanas de aviso propias, con el mismo diseño que el resto de la app.

Reemplazan a las ventanas grises del sistema (QMessageBox / QInputDialog):
  info · success · warn · error   → aviso con un botón
  ask                             → sí / no (con variante «peligrosa» en rojo)
  ask_typed                       → para acciones que no se pueden deshacer: hay que escribir una palabra
  ask_text                        → pedir un dato (ej. el código de 6 dígitos de Apple)
Se cierran con Esc, se confirman con Enter y se pueden arrastrar.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRegularExpression, Qt
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from . import icons, theme

KINDS = {
    #            ícono       color         fondo del círculo
    "info":     ("info",     theme.ACCENT_HOVER, "#16244a"),
    "success":  ("check",    theme.GREEN,  "#123227"),
    "warn":     ("warn",     theme.ORANGE, "#33280f"),
    "error":    ("error",    theme.RED,    "#3a1620"),
    "question": ("question", theme.ACCENT_HOVER, "#16244a"),
    "danger":   ("warn",     theme.RED,    "#3a1620"),
    "lock":     ("lock",     theme.ACCENT_HOVER, "#16244a"),
    "shield":   ("shield",   theme.GREEN,  "#123227"),
}

DIALOG_QSS = f"""
QFrame#dlgCard {{ background: {theme.CARD}; border: 1px solid {theme.BORDER}; border-radius: 16px; }}
QLabel#dlgTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#dlgText {{ color: #c9cde6; font-size: 13px; }}
QPlainTextEdit#dlgDetails {{ background: #0f1328; color: {theme.MUTED}; font-size: 12px; }}
QPushButton#dlgClose {{ background: transparent; border: none; padding: 4px; border-radius: 6px; }}
QPushButton#dlgClose:hover {{ background: {theme.CARD_2}; }}
QLineEdit#dlgInput {{ font-size: 15px; padding: 9px 10px; }}
QLineEdit#dlgCode {{ font-size: 24px; letter-spacing: 10px; padding: 8px 10px; font-weight: 600; }}
"""


class VaultDialog(QDialog):
    """Base: tarjeta flotante con ícono, título, texto y botones."""

    def __init__(self, parent: Optional[QWidget], kind: str, title: str, text: str = "",
                 details: Optional[str] = None, width: int = 480):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setWindowTitle(title)
        self.result_key: Optional[str] = None
        self._drag: Optional[QPoint] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 22, 22, 22)
        self.card = QFrame()
        self.card.setObjectName("dlgCard")
        self.card.setStyleSheet(DIALOG_QSS)
        self.card.setFixedWidth(width)
        sh = QGraphicsDropShadowEffect(self.card)
        sh.setBlurRadius(40)
        sh.setOffset(0, 10)
        sh.setColor(QColor(0, 0, 0, 170))
        self.card.setGraphicsEffect(sh)
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        icon_name, color, bg = KINDS.get(kind, KINDS["info"])
        head = QHBoxLayout()
        head.setSpacing(14)
        badge = QLabel()
        badge.setFixedSize(42, 42)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background:{bg}; border-radius:21px;")
        badge.setPixmap(icons.pixmap(icon_name, color, 22, 2.0))
        head.addWidget(badge, 0, Qt.AlignTop)
        tcol = QVBoxLayout()
        tcol.setSpacing(4)
        t = QLabel(title)
        t.setObjectName("dlgTitle")
        t.setWordWrap(True)
        tcol.addWidget(t)
        if text:
            body = QLabel(text)
            body.setObjectName("dlgText")
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            tcol.addWidget(body)
        head.addLayout(tcol, 1)
        close = QPushButton()
        close.setObjectName("dlgClose")
        close.setIcon(icons.icon("close", theme.MUTED, 16))
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.reject)
        head.addWidget(close, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self.body.setContentsMargins(56, 0, 0, 0)
        lay.addLayout(self.body)

        if details:
            self._details = QPlainTextEdit(details)
            self._details.setObjectName("dlgDetails")
            self._details.setReadOnly(True)
            self._details.setMaximumHeight(160)
            self._details.hide()
            toggle = QPushButton("Ver detalles")
            toggle.setObjectName("link")
            toggle.setCursor(Qt.PointingHandCursor)

            def flip():
                vis = not self._details.isVisible()
                self._details.setVisible(vis)
                toggle.setText("Ocultar detalles" if vis else "Ver detalles")
                self.adjustSize()
            toggle.clicked.connect(flip)
            self.body.addWidget(toggle, 0, Qt.AlignLeft)
            self.body.addWidget(self._details)

        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        lay.addSpacing(4)
        lay.addLayout(self.buttons)

    def add_button(self, key: str, text: str, role: Optional[str] = None, default: bool = False) -> QPushButton:
        b = QPushButton(text)
        if role:
            b.setObjectName(role)
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(34)
        b.setMinimumWidth(96)
        b.clicked.connect(lambda: self._finish(key))
        b.setAutoDefault(default)
        b.setDefault(default)
        self.buttons.addWidget(b)
        if default:
            b.setFocus()
        return b

    def _finish(self, key: str):
        self.result_key = key
        self.accept()

    # arrastrar la tarjeta
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def run(self) -> Optional[str]:
        self.adjustSize()
        par = self.parentWidget()
        if par is not None and par.isVisible():
            g = par.window().frameGeometry()
            self.move(g.center() - self.rect().center())
        self.exec()
        return self.result_key


# ---------------------------------------------------------------- atajos
def _notify(parent, kind, title, text, details=None, ok="Entendido") -> None:
    d = VaultDialog(parent, kind, title, text, details)
    d.add_button("ok", ok, "primary", default=True)
    d.run()


def info(parent, title: str, text: str = "", details: Optional[str] = None, ok: str = "Entendido"):
    _notify(parent, "info", title, text, details, ok)


def success(parent, title: str, text: str = "", details: Optional[str] = None, ok: str = "Listo"):
    _notify(parent, "success", title, text, details, ok)


def warn(parent, title: str, text: str = "", details: Optional[str] = None, ok: str = "Entendido"):
    _notify(parent, "warn", title, text, details, ok)


def error(parent, title: str, text: str = "", details: Optional[str] = None, ok: str = "Cerrar"):
    _notify(parent, "error", title, text, details, ok)


def ask(parent, title: str, text: str = "", yes: str = "Continuar", no: str = "Cancelar",
        danger: bool = False, kind: Optional[str] = None, details: Optional[str] = None) -> bool:
    d = VaultDialog(parent, kind or ("danger" if danger else "question"), title, text, details)
    d.add_button("no", no)
    d.add_button("yes", yes, "danger" if danger else "primary", default=not danger)
    return d.run() == "yes"


def choose(parent, title: str, text: str, options: list[tuple[str, str]], kind: str = "question",
           cancel: str = "Cancelar") -> Optional[str]:
    """Elegir una opción entre varias (botones). Devuelve la clave elegida o None."""
    d = VaultDialog(parent, kind, title, text)
    d.add_button("__cancel__", cancel)
    for i, (key, label_) in enumerate(options):
        d.add_button(key, label_, "primary" if i == 0 else None, default=(i == 0))
    r = d.run()
    return None if r in (None, "__cancel__") else r


def ask_typed(parent, title: str, text: str, word: str = "BORRAR", yes: str = "Confirmar") -> bool:
    """Confirmación fuerte: el botón se habilita recién al escribir la palabra."""
    d = VaultDialog(parent, "danger", title, text)
    hint = QLabel(f"Para confirmar, escribí <b>{word}</b>:")
    hint.setObjectName("dlgText")
    d.body.addWidget(hint)
    field = QLineEdit()
    field.setObjectName("dlgInput")
    field.setPlaceholderText(word)
    d.body.addWidget(field)
    d.add_button("no", "Cancelar", default=True)
    b = d.add_button("yes", yes, "danger")
    b.setEnabled(False)
    field.textChanged.connect(lambda s: b.setEnabled(s.strip().upper() == word.upper()))
    field.returnPressed.connect(lambda: b.isEnabled() and d._finish("yes"))  # noqa: SLF001
    field.setFocus()
    return d.run() == "yes" and field.text().strip().upper() == word.upper()


def ask_text(parent, title: str, text: str, placeholder: str = "", ok: str = "Aceptar",
             digits: Optional[int] = None, kind: str = "lock") -> Optional[str]:
    """Pide un dato. Con digits=6 muestra un campo grande que solo acepta 6 números (códigos de Apple)."""
    d = VaultDialog(parent, kind, title, text)
    field = QLineEdit()
    field.setPlaceholderText(placeholder or ("•" * digits if digits else ""))
    if digits:
        field.setObjectName("dlgCode")
        field.setAlignment(Qt.AlignCenter)
        field.setMaxLength(digits)
        field.setValidator(QRegularExpressionValidator(QRegularExpression(rf"\d{{0,{digits}}}")))
    else:
        field.setObjectName("dlgInput")
    d.body.addWidget(field)
    d.add_button("no", "Cancelar")
    b = d.add_button("yes", ok, "primary", default=True)
    if digits:
        b.setEnabled(False)
        field.textChanged.connect(lambda s: b.setEnabled(len(s) == digits))
    field.returnPressed.connect(lambda: b.isEnabled() and d._finish("yes"))  # noqa: SLF001
    field.setFocus()
    if d.run() != "yes":
        return None
    return field.text().strip() or None
