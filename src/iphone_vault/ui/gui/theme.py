"""Tema visual oscuro (azul noche + acento azul) y utilidades de formato en español."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

BG = "#0d1020"
SIDEBAR = "#0a0c18"
CARD = "#161a2e"
CARD_2 = "#1d2240"
BORDER = "#272d4d"
TEXT = "#e8eaf6"
MUTED = "#9aa1c4"
ACCENT = "#2f6bff"
ACCENT_HOVER = "#4a80ff"
GREEN = "#3ddc84"
RED = "#ff5c6c"
ORANGE = "#ffb547"

FONT = '"Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", Arial, sans-serif'

QSS = f"""
* {{ font-family: {FONT}; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root, QStackedWidget, QWidget#page {{ background: {BG}; }}
QWidget#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 19px; font-weight: 700; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 11px; }}
QPushButton#nav {{
    text-align: left; padding: 10px 14px; border: none; border-radius: 9px;
    background: transparent; color: {MUTED}; font-size: 13.5px;
}}
QPushButton#nav:hover {{ background: {CARD}; color: {TEXT}; }}
QPushButton#nav:checked {{ background: {CARD_2}; color: {TEXT}; font-weight: 600; }}
QLabel#chip {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 9px; color: {MUTED}; }}
QLabel#h1 {{ font-size: 26px; font-weight: 700; }}
QLabel#h2 {{ font-size: 17px; font-weight: 600; }}
QLabel#muted, QLabel#small {{ color: {MUTED}; }}
QLabel#small {{ font-size: 12px; }}
QLabel#big {{ font-size: 22px; font-weight: 600; }}
QLabel#live {{ color: {GREEN}; font-size: 12px; }}
QLabel#liveOff {{ color: {MUTED}; font-size: 12px; }}
QLabel#value {{ color: {TEXT}; }}
QLabel#bad {{ color: {RED}; }}
QLabel#good {{ color: {GREEN}; }}
QFrame#tile {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#tile:hover {{ border-color: #3a4370; background: #191e36; }}
QLabel#tileValue {{ font-size: 19px; font-weight: 700; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#inner {{ background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#sep {{ background: {BORDER}; max-height: 1px; min-height: 1px; }}
QLabel#banner {{ background: #2b2213; color: #ffd28a; border: 1px solid #5a4520; border-radius: 10px; padding: 10px 12px; }}
QLabel#info {{ background: #13203f; color: #bcd0ff; border: 1px solid #263d74; border-radius: 10px; padding: 10px 12px; }}
QPushButton {{
    background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 16px;
}}
QPushButton:hover {{ background: #262c52; }}
QPushButton:disabled {{ color: #5b6187; background: #14172a; border-color: #1f2440; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; font-weight: 600; color: white; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #22305e; border-color: #22305e; color: #8090c0; }}
QPushButton#danger {{ background: #4a1620; border: 1px solid #7a2433; color: #ffd0d5; font-weight: 600; }}
QPushButton#danger:hover {{ background: #5e1b28; }}
QPushButton#danger:disabled {{ background: #2a1419; border-color: #3d1d25; color: #8a6b72; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT_HOVER}; padding: 2px; }}
QProgressBar {{ background: #262b4a; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QLineEdit, QSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QListWidget {{
    background: #0f1328; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {ACCENT};
}}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #4a5285; background: #0f1328; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; margin-top: 14px; padding: 16px 14px 12px 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 4px; color: {TEXT}; font-weight: 600; }}
QTableView, QTableWidget {{
    background: {CARD}; alternate-background-color: #191e36; border: 1px solid {BORDER}; border-radius: 12px;
    gridline-color: transparent; selection-background-color: #2a3a78; selection-color: white;
}}
QHeaderView::section {{ background: {CARD_2}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 7px 8px; font-weight: 600; }}
QTableCornerButton::section {{ background: {CARD_2}; border: none; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2c3360; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2c3360; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{ background: {CARD_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
QMessageBox, QDialog, QInputDialog {{ background: {CARD}; }}
"""


def num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def size(n: Optional[float]) -> str:
    if n is None:
        return "—"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            if unit == "B":
                return f"{v:.0f} B"
            return f"{v:.1f} {unit}".replace(".", ",")
        v /= 1024
    return f"{n} B"


def gb_pair(done: int, total: int) -> str:
    """"182 de 285 GB" (con la unidad del total)."""
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if total >= div:
            fmt = (lambda x: f"{x / div:.0f}") if total / div >= 100 else (lambda x: f"{x / div:.1f}".replace(".", ","))
            return f"{fmt(done)} de {fmt(total)} {unit}"
    return f"{done} de {total} B"


def eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "calculando…"
    s = int(seconds)
    if s < 60:
        return "<1 min restante"
    m = s // 60
    if m < 60:
        return f"~{m} min restantes"
    h, m = divmod(m, 60)
    return f"~{h} h {m:02d} min restantes"


def ago(when: Optional[datetime]) -> str:
    if when is None:
        return "nunca"
    s = int((datetime.now() - when).total_seconds())
    if s < 60:
        return "recién"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return f"hace {s // 86400} días"
