"""Centro de alertas: historial de avisos guardado en la configuración de la app (no en la carpeta de fotos).

Cada alerta: fecha, nivel (success/info/warn/error), título, texto, si ya se leyó. Se guardan las últimas 300.
Los textos pasan por el mismo enmascarado que el registro (sin correos ni códigos).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from ...config import config_dir
from ...security.logs import mask
from . import icons, theme
from .dialogs import KINDS

MAX_ALERTS = 300


@dataclass
class Alert:
    level: str
    title: str
    text: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    read: bool = False

    @property
    def when(self) -> datetime:
        try:
            return datetime.fromisoformat(self.at)
        except ValueError:
            return datetime.now()


class AlertStore(QObject):
    changed = Signal()

    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self.path = path or (config_dir() / "alertas.dat")
        self.items: list[Alert] = []
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Alert(**{k: v for k, v in a.items() if k in Alert.__dataclass_fields__})
                          for a in raw if isinstance(a, dict)]
        except (OSError, ValueError, TypeError):
            self.items = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(a) for a in self.items[-MAX_ALERTS:]], ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, level: str, title: str, text: str = "") -> Alert:
        a = Alert(level, mask(title), mask(text))
        self.items.append(a)
        self.items = self.items[-MAX_ALERTS:]
        self._save()
        self.changed.emit()
        return a

    def unread(self) -> int:
        return sum(1 for a in self.items if not a.read)

    def mark_all_read(self):
        if any(not a.read for a in self.items):
            for a in self.items:
                a.read = True
            self._save()
            self.changed.emit()

    def clear(self):
        self.items = []
        self._save()
        self.changed.emit()


class _AlertRow(QFrame):
    def __init__(self, a: Alert):
        super().__init__()
        self.setObjectName("alertRow")
        icon_name, color, bg = KINDS.get(a.level, KINDS["info"])
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        badge = QLabel()
        badge.setFixedSize(34, 34)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background:{bg}; border-radius:17px;")
        badge.setPixmap(icons.pixmap(icon_name, color, 18, 2.0))
        lay.addWidget(badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        t = QLabel(a.title)
        t.setStyleSheet("font-weight:600;" + ("" if a.read else f"color:{theme.TEXT};"))
        t.setWordWrap(True)
        top.addWidget(t, 1)
        if not a.read:
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background:{theme.ACCENT_HOVER}; border-radius:4px;")
            top.addWidget(dot, 0, Qt.AlignVCenter)
        w = QLabel(theme.ago(a.when))
        w.setStyleSheet(f"color:{theme.MUTED}; font-size:11.5px;")
        top.addWidget(w, 0, Qt.AlignTop)
        col.addLayout(top)
        if a.text:
            b = QLabel(a.text)
            b.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            b.setStyleSheet(f"color:{theme.MUTED}; font-size:12.5px;")
            col.addWidget(b)
        lay.addLayout(col, 1)
        self.setStyleSheet(f"QFrame#alertRow {{ background:{theme.CARD}; border:1px solid {theme.BORDER}; "
                           f"border-radius:12px; }}")


class AlertsPanel(QWidget):
    """Lista de alertas para la página «Alertas»."""

    def __init__(self, store: AlertStore):
        super().__init__()
        self.store = store
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        bar = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("muted")
        bar.addWidget(self.summary, 1)
        b1 = QPushButton("Marcar todo como leído")
        b1.clicked.connect(store.mark_all_read)
        b2 = QPushButton("Vaciar historial")
        b2.clicked.connect(store.clear)
        for b in (b1, b2):
            b.setCursor(Qt.PointingHandCursor)
            bar.addWidget(b)
        lay.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        holder.setObjectName("page")
        self.rows = QVBoxLayout(holder)
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.setSpacing(8)
        self.rows.addStretch(1)
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll, 1)
        self.empty = QLabel("Sin alertas por ahora. Acá vas a ver cuándo terminó cada copia, los errores, "
                            "los avisos del disco y los resultados de las verificaciones.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.empty)
        store.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        while self.rows.count() > 1:
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for i, a in enumerate(list(reversed(self.store.items))[:150]):
            self.rows.insertWidget(i, _AlertRow(a))
        n, u = len(self.store.items), self.store.unread()
        self.summary.setText(f"{n} alertas · {u} sin leer" if n else "")
        self.empty.setVisible(not n)
        self.scroll.setVisible(bool(n))
