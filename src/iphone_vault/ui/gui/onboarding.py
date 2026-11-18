"""Guía de primer uso: 4 pasos cortos (qué hace, dónde guardar, cómo conectar el celular, listo)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from ... import APP_NAME
from ...system.disk import disk_info
from . import icons, theme
from .dialogs import DIALOG_QSS


def _feature(icon: str, color: str, title: str, text: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 4, 0, 4)
    h.setSpacing(14)
    b = QLabel()
    b.setFixedSize(40, 40)
    b.setAlignment(Qt.AlignCenter)
    b.setStyleSheet(f"background:{theme.CARD_2}; border:1px solid {theme.BORDER}; border-radius:11px;")
    b.setPixmap(icons.pixmap(icon, color, 20, 1.9))
    h.addWidget(b, 0, Qt.AlignTop)
    c = QVBoxLayout()
    c.setSpacing(2)
    t = QLabel(title)
    t.setStyleSheet("font-weight:600; font-size:14px;")
    c.addWidget(t)
    d = QLabel(text)
    d.setWordWrap(True)
    d.setStyleSheet(f"color:{theme.MUTED};")
    c.addWidget(d)
    h.addLayout(c, 1)
    return w


def _step(n: str, text: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(12)
    num = QLabel(n)
    num.setFixedSize(26, 26)
    num.setAlignment(Qt.AlignCenter)
    num.setStyleSheet(f"background:{theme.ACCENT}; color:white; border-radius:13px; font-weight:700;")
    h.addWidget(num, 0, Qt.AlignTop)
    t = QLabel(text)
    t.setWordWrap(True)
    t.setTextFormat(Qt.RichText)
    h.addWidget(t, 1)
    return w


class OnboardingDialog(QDialog):
    def __init__(self, parent, choose_folder: Callable[[], Optional[str]], current_dest: str = "",
                 test_limit: int = 10):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._choose = choose_folder
        self.dest = current_dest

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = QFrame()
        card.setObjectName("dlgCard")
        card.setStyleSheet(DIALOG_QSS)
        card.setFixedWidth(620)
        sh = QGraphicsDropShadowEffect(card)
        sh.setBlurRadius(44)
        sh.setOffset(0, 12)
        sh.setColor(QColor(0, 0, 0, 180))
        card.setGraphicsEffect(sh)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(16)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        lay.addWidget(self.stack, 1)

        # 1 · bienvenida
        p1, l1 = self._page("Bienvenido a " + APP_NAME, "Copiá todas tus fotos y videos del celular (iPhone, "
                                                          "Samsung o cualquier Android) a esta compu, en calidad original.")
        l1.addWidget(_feature("phone", theme.ACCENT_HOVER, "Por cable, sin internet",
                              "La copia va directo del celular a tu disco. La app bloquea cualquier conexión a "
                              "internet que no hayas pedido."))
        l1.addWidget(_feature("shield", theme.GREEN, "Verificado archivo por archivo",
                              "Cada foto se compara byte a byte (SHA-256) antes de darla por copiada. Si se corta "
                              "el cable, sigue donde quedó."))
        l1.addWidget(_feature("lock", theme.ORANGE, "Nada sale de tu compu",
                              "Las fotos quedan solo en la carpeta que elijas. Sin cuentas, sin servidores de "
                              "terceros."))
        # 2 · carpeta
        p2, l2 = self._page("¿Dónde guardamos todo?", "Elegí una carpeta en un disco con espacio. Se crea una "
                                                       "subcarpeta por año (2023, 2024…).")
        row = QHBoxLayout()
        self.dest_lbl = QLabel()
        self.dest_lbl.setWordWrap(True)
        self.dest_lbl.setStyleSheet(f"background:#0f1328; border:1px solid {theme.BORDER}; border-radius:9px; "
                                    "padding:10px 12px;")
        row.addWidget(self.dest_lbl, 1)
        pick = QPushButton("Elegir carpeta…")
        pick.setObjectName("primary")
        pick.setCursor(Qt.PointingHandCursor)
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        l2.addLayout(row)
        self.disk_lbl = QLabel()
        self.disk_lbl.setWordWrap(True)
        l2.addWidget(self.disk_lbl)
        tip = QLabel("Consejo: evitá carpetas de OneDrive, Google Drive o Dropbox si no querés que las fotos se "
                     "suban a esa nube. Un disco externo en formato NTFS o exFAT es ideal.")
        tip.setWordWrap(True)
        tip.setObjectName("info")
        l2.addWidget(tip)
        # 3 · celular
        p3, l3 = self._page("Conectá tu celular", "Hacelo una sola vez y después la app lo reconoce sola.")
        l3.addWidget(_step("1", "Enchufá el celular a la compu con el <b>cable</b> (mejor directo, sin hub) y "
                                "<b>desbloquealo</b>."))
        l3.addWidget(_step("2", "<b>iPhone:</b> tocá «Confiar en esta computadora» e ingresá tu código.<br>"
                                "<b>Samsung / Android:</b> en el aviso «USB» de la barra de notificaciones elegí "
                                "<b>«Transferencia de archivos»</b>."))
        l3.addWidget(_step("3", "En el inicio tocá <b>«Conectar celular»</b>. Mientras copia, la compu no se "
                                "suspende. ¿Tarjeta SD o cámara? Usá «Tarjeta SD o carpeta…»."))
        note = QLabel("¿No aparece? En Windows, el iPhone necesita el controlador de Apple («Iniciar Cloudy.bat» lo "
                      "instala solo). En Mac, Android se conecta con «Depuración USB» (la app te guía).")
        note.setWordWrap(True)
        note.setObjectName("info")
        l3.addWidget(note)
        # 4 · listo
        p4, l4 = self._page("¡Todo listo!", "")
        msg = (f"La primera copia es <b>de prueba: {test_limit} elementos</b>, para que veas el resultado. Cuando "
               "te convenza, en <b>Ajustes</b> poné el límite en 0 para copiar todo." if test_limit else
               "Tocá <b>«Copiar ahora»</b> y listo.")
        l4.addWidget(_step("✓", msg))
        l4.addWidget(_step("✓", "Si alguna foto está <b>optimizada</b> (el original está en iCloud), queda en "
                                "<b>Pendientes</b> para bajarla después sin repetir nada."))
        l4.addWidget(_step("✓", "Cada tanto, en <b>Salud</b>, tocá <b>«Verificar todo»</b>: confirma que cada "
                                "archivo sigue intacto en el disco."))

        for p in (p1, p2, p3, p4):
            self.stack.addWidget(p)

        foot = QHBoxLayout()
        self.dots = []
        for _ in range(4):
            d = QLabel()
            d.setFixedSize(8, 8)
            self.dots.append(d)
            foot.addWidget(d)
        foot.addStretch(1)
        self.skip = QPushButton("Saltar guía")
        self.skip.setObjectName("link")
        self.skip.setCursor(Qt.PointingHandCursor)
        self.skip.clicked.connect(self.accept)
        self.back = QPushButton("Atrás")
        self.back.setCursor(Qt.PointingHandCursor)
        self.back.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next = QPushButton("Siguiente")
        self.next.setObjectName("primary")
        self.next.setCursor(Qt.PointingHandCursor)
        self.next.setMinimumWidth(120)
        self.next.clicked.connect(self._next)
        for b in (self.skip, self.back, self.next):
            b.setMinimumHeight(34)
            foot.addWidget(b)
        lay.addLayout(foot)
        self._refresh_dest()
        self._go(0)

    def _page(self, title: str, subtitle: str):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        t = QLabel(title)
        t.setStyleSheet("font-size:22px; font-weight:700;")
        v.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setWordWrap(True)
            s.setStyleSheet(f"color:{theme.MUTED}; font-size:13.5px;")
            v.addWidget(s)
        v.addSpacing(4)
        inner = QVBoxLayout()
        inner.setSpacing(10)
        v.addLayout(inner)
        v.addStretch(1)
        return w, inner

    def _go(self, i: int):
        i = max(0, min(3, i))
        self.stack.setCurrentIndex(i)
        for k, d in enumerate(self.dots):
            d.setStyleSheet(f"background:{theme.ACCENT if k == i else theme.BORDER}; border-radius:4px;")
        self.back.setVisible(i > 0)
        self.skip.setVisible(i < 3)
        self.next.setText("Empezar" if i == 3 else "Siguiente")
        self.next.setEnabled(not (i == 1 and not self.dest))

    def _next(self):
        i = self.stack.currentIndex()
        if i == 3:
            self.accept()
        else:
            self._go(i + 1)

    def _pick(self):
        p = self._choose()
        if p:
            self.dest = p
        self._refresh_dest()
        self._go(self.stack.currentIndex())

    def _refresh_dest(self):
        if not self.dest:
            self.dest_lbl.setText("Todavía no elegiste una carpeta.")
            self.disk_lbl.setText("")
            return
        self.dest_lbl.setText(self.dest)
        di = disk_info(self.dest)
        if not di:
            self.disk_lbl.setText("")
            return
        txt = f"Espacio libre: {theme.size(di.free)} de {theme.size(di.total)}"
        if di.is_fat:
            txt += ("  ·  ⚠ Este disco es FAT32: no acepta archivos de más de 4 GB (videos largos en 4K). "
                    "Mejor uno en NTFS o exFAT.")
            self.disk_lbl.setStyleSheet(f"color:{theme.ORANGE};")
        elif di.low_space:
            txt += "  ·  ⚠ Queda poco espacio."
            self.disk_lbl.setStyleSheet(f"color:{theme.ORANGE};")
        else:
            self.disk_lbl.setStyleSheet(f"color:{theme.GREEN};")
        self.disk_lbl.setText(txt)

    def run(self) -> bool:
        self.adjustSize()
        par = self.parentWidget()
        if par is not None and par.isVisible():
            self.move(par.window().frameGeometry().center() - self.rect().center())
        return self.exec() == QDialog.Accepted
