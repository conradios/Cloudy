"""Interfaz gráfica de Cloudy (PySide6) — panel en vivo, fototeca, pendientes, liberar espacio y ajustes."""
from __future__ import annotations

import copy
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QDate, QPoint, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMenu, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
                               QStackedWidget, QSystemTrayIcon, QTableView, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ... import APP_NAME, __version__
from ...catalog.manifest import STATUS_EXCLUDED, STATUS_FAILED, Manifest
from ...config import LAYOUT_LABELS, AppSettings, TransferOptions, config_dir, load_settings, save_settings
from ...device.base import DeviceBackend, DeviceError
from ...device.detect import detect_devices
from ...models import AVAIL_CLOUD_ONLY, AVAIL_OPTIMIZED, PHOTO_EXT, VIDEO_EXT, MediaItem
from ...scanner.generic_scanner import scan_backend
from ...security import no_network
from ...security.hotspot import MESSAGE as HOTSPOT_MESSAGE
from ...security.hotspot import hotspot_interfaces
from ...transfer.cleanup import CleanupCandidate, delete_from_icloud, find_candidates
from ...organizer.reorganize import count_pending, count_sidecars, reorganize
from ...transfer.compat import COMPAT_DIR, compat_missing_libs, compat_rel, convert_existing
from ...transfer.engine import (STATE_CONVERTING, STATE_PAUSED, STATE_WAITING, Plan, Progress, TransferEngine, build_plan,
                                passes_filters, register_unavailable, wanted_files)
from ...catalog.backup import list_backups
from ...security.logs import export_logs, logs_dir
from ...system.disk import disk_info, too_big_for_fat
from ...system.power import KeepAwake
from ...transfer.verify import last_verification, repair, verify_library
from . import dialogs as dlg
from . import icons, theme
from .brand import app_icon, logo_pixmap
from .alerts import AlertsPanel, AlertStore
from .onboarding import OnboardingDialog
from .toasts import ToastHost
from .library_model import (FILTERS, ST_DELETED, ST_DONE, ST_EXCLUDED, ST_FAILED, ST_LIMIT, ST_OPTIMIZED,
                            ST_PENDING, ST_TRASH, ST_USER_EXCLUDED, ST_WILL, ST_WILL_CLOUD, LibraryModel,
                            LibraryProxy, Row, merge_rows)

log = logging.getLogger(__name__)

P_HOME, P_LIBRARY, P_PENDING, P_CLEANUP, P_HEALTH, P_ALERTS, P_SETTINGS, P_LOG = range(8)
NAV = [("Inicio", "home"), ("Fototeca", "photos"), ("Pendientes", "clock"), ("Liberar espacio", "broom"),
       ("Salud", "shield"), ("Alertas", "bell"), ("Ajustes", "gear"), ("Registro", "list")]

CLOUD_FOLDER_HINTS = ("onedrive", "google drive", "googledrive", "my drive", "mi unidad", "dropbox", "icloud drive",
                      "iclouddrive", "box sync", "mega")


# ====================================================================== helpers
def card(title: Optional[str] = None, icon: Optional[str] = None,
         icon_color: str = theme.ACCENT_HOVER) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    if title:
        row = QHBoxLayout()
        row.setSpacing(10)
        if icon:
            b = QLabel()
            b.setFixedSize(30, 30)
            b.setAlignment(Qt.AlignCenter)
            b.setStyleSheet(f"background:{theme.CARD_2}; border:1px solid {theme.BORDER}; border-radius:8px;")
            b.setPixmap(icons.pixmap(icon, icon_color, 16, 1.9))
            row.addWidget(b)
        t = QLabel(title)
        t.setObjectName("h2")
        row.addWidget(t, 1)
        lay.addLayout(row)
    return f, lay


class Tile(QFrame):
    """Mosaico de estado clickeable (Inicio): ícono, título, valor grande y detalle."""

    def __init__(self, icon: str, title: str, on_click: Optional[Callable] = None):
        super().__init__()
        self.setObjectName("tile")
        self.setCursor(Qt.PointingHandCursor if on_click else Qt.ArrowCursor)
        self._click = on_click
        self._icon = icon
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.badge = QLabel()
        self.badge.setFixedSize(30, 30)
        self.badge.setAlignment(Qt.AlignCenter)
        top.addWidget(self.badge)
        t = label(title, "muted")
        top.addWidget(t, 1)
        lay.addLayout(top)
        self.value = label("—", "tileValue")
        lay.addWidget(self.value)
        self.detail = label("", "small", wrap=True)
        lay.addWidget(self.detail)
        self.set_tone("neutral")

    def set_tone(self, tone: str):
        color = {"good": theme.GREEN, "warn": theme.ORANGE, "bad": theme.RED}.get(tone, theme.ACCENT_HOVER)
        bg = {"good": "#123227", "warn": "#33280f", "bad": "#3a1620"}.get(tone, "#16244a")
        self.badge.setStyleSheet(f"background:{bg}; border-radius:15px;")
        self.badge.setPixmap(icons.pixmap(self._icon, color, 16, 2.0))

    def set(self, value: str, detail: str = "", tone: str = "neutral"):
        self.value.setText(value)
        self.detail.setText(detail)
        self.set_tone(tone)

    def mouseReleaseEvent(self, e):
        if self._click and e.button() == Qt.LeftButton:
            self._click()
        super().mouseReleaseEvent(e)


def label(text: str = "", name: Optional[str] = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def button(text: str, name: Optional[str] = None, slot: Optional[Callable] = None) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


def page(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    w.setObjectName("page")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(28, 24, 28, 24)
    lay.setSpacing(14)
    lay.addWidget(label(title, "h1"))
    if subtitle:
        lay.addWidget(label(subtitle, "muted", wrap=True))
    return w, lay


def scrollable(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setFrameShape(QFrame.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    s.setWidget(w)
    return s


def set_role(w: QWidget, name: str):
    """Cambia el estilo (objectName) y repinta SOLO si cambió: repulir estilos en cada tick es caro."""
    if w.objectName() != name:
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)


def _system_exe(name: str) -> Optional[str]:
    """Ruta ABSOLUTA de un programa del sistema (nunca se busca en la carpeta actual: evita que un archivo
    plantado con ese nombre se ejecute)."""
    if sys.platform == "win32":
        p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / name
        return str(p) if p.exists() else None
    for d in ("/usr/bin", "/bin", "/usr/local/bin"):
        p = Path(d) / name
        if p.exists():
            return str(p)
    return None


def reveal_in_folder(path: Path):
    path = Path(path).resolve()
    if sys.platform == "win32":
        exe = _system_exe("explorer.exe")
        if exe:
            subprocess.Popen([exe, "/select,", str(path)])  # nosec B603 - ruta absoluta, sin shell
    elif sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-R", str(path)])  # nosec B603
    else:
        exe = _system_exe("xdg-open")
        if exe:
            subprocess.Popen([exe, str(path.parent)])  # nosec B603


SAFE_TO_OPEN = PHOTO_EXT | VIDEO_EXT | {".jpg", ".jpeg", ".mp4", ".png", ".zip", ".log", ".txt"}


def open_path(path: Path):
    """Abre una carpeta o una foto/video. Cualquier otro tipo de archivo NO se abre (se muestra en la carpeta):
    así nunca se ejecuta un programa por error."""
    path = Path(path).resolve()
    if path.is_file() and path.suffix.lower() not in SAFE_TO_OPEN:
        reveal_in_folder(path)
        return
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606  # nosec B606 - carpeta o archivo multimedia
    elif sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", str(path)])  # nosec B603
    else:
        exe = _system_exe("xdg-open")
        if exe:
            subprocess.Popen([exe, str(path)])  # nosec B603


def is_cloud_folder(path: str) -> bool:
    low = path.lower().replace("\\", "/")
    envs = [os.environ.get(k, "") for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    if any(e and low.startswith(e.lower().replace("\\", "/")) for e in envs):
        return True
    return any(h in low for h in CLOUD_FOLDER_HINTS)


class TaskThread(QThread):
    """Ejecuta una función larga fuera del hilo de la interfaz."""
    status = Signal(str)
    progress = Signal(object)
    log_line = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[["TaskThread"], object], parent=None):
        super().__init__(parent)
        self.fn = fn
        self.completed = False

    def run(self):
        try:
            result = self.fn(self)
        except DeviceError as e:
            self.completed = True
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            log.error("Error en tarea: %s", traceback.format_exc())
            self.completed = True
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.completed = True
            self.done.emit(result)


class ICloudLoginDialog(dlg.VaultDialog):
    def __init__(self, parent, apple_id: str, demo: bool):
        super().__init__(parent, "lock", "Conectar iCloud Fotos",
                         "Tu Apple ID y contraseña van directo a Apple: no pasan por ningún otro servidor. "
                         "La contraseña no se guarda; solo queda en esta compu la sesión que entrega Apple para no "
                         "pedirte el código cada vez. Con iCloud activado, la app solo puede conectarse a dominios "
                         "de Apple.", width=520)
        lay = self.body
        if demo:
            lay.addWidget(label("Modo demo: usá cualquier contraseña. El código de 2 pasos es 123456.", "banner", True))
        form = QFormLayout()
        self.apple_id = QLineEdit(apple_id)
        self.apple_id.setPlaceholderText("tucorreo@icloud.com")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("Apple ID:", self.apple_id)
        form.addRow("Contraseña:", self.password)
        lay.addLayout(form)
        lay.addWidget(label("Requisitos: «Acceso a los datos de iCloud en la web» activado y sin «Protección de "
                            "datos avanzada» (Ajustes > [tu nombre] > iCloud).", "small", wrap=True))
        self.add_button("no", "Cancelar")
        self.add_button("yes", "Iniciar sesión", "primary", default=True)
        self.password.returnPressed.connect(lambda: self._finish("yes"))
        (self.password if apple_id else self.apple_id).setFocus()


# ================================================================== ventana
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings: AppSettings = load_settings()
        self.manifest: Optional[Manifest] = None
        self.usb: Optional[DeviceBackend] = None
        self.usb_label = ""
        self.usb_items: list[MediaItem] = []
        self.usb_scan_time: Optional[datetime] = None
        self.usb_warnings: list[str] = []
        self.icloud: Optional[DeviceBackend] = None
        self.icloud_account = None
        self.icloud_items: list[MediaItem] = []
        self.icloud_list_time: Optional[datetime] = None
        self.plans: dict[str, Plan] = {}
        self.rows: list[Row] = []
        self.cleanup_candidates: list[CleanupCandidate] = []
        self.task: Optional[TaskThread] = None
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.syncing = False
        self.session_errors = 0
        self.last_progress: Optional[Progress] = None
        self.phase = ""
        self._probe = None
        self._auto_found = None
        self._auto_thread: Optional[threading.Thread] = None
        self.verify_report = None
        self.alerts = AlertStore()
        self._low_space_warned = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1320, 880)
        self.setMinimumSize(1080, 720)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self._refresh_all)

        self._build()
        self.toasts = ToastHost(self.centralWidget())
        self._setup_tray()
        self.alerts.changed.connect(self._refresh_alert_badge)
        self._refresh_alert_badge()
        self._load_settings_into_ui()
        self._open_manifest()
        self._refresh_all()
        self._go(P_HOME)
        QTimer.singleShot(400, self._startup_checks)

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(5000)
        self._auto_timer.timeout.connect(self._auto_tick)
        self._auto_timer.start()

    # ================================================================ estructura
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # ---------------- barra lateral
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(230)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 18, 14, 16)
        sv.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(36, 36)
        logo.setPixmap(logo_pixmap(36))
        brand.addWidget(logo)
        bcol = QVBoxLayout()
        bcol.setSpacing(0)
        bcol.addWidget(label(APP_NAME, "brand"))
        bcol.addWidget(label(f"v{__version__} · tu nube, en tu compu", "brandSub"))
        brand.addLayout(bcol)
        sv.addLayout(brand)
        sv.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for i, (text, ic) in enumerate(NAV):
            b = QPushButton("  " + text)
            b.setObjectName("nav")
            b.setIcon(icons.icon(ic, theme.MUTED, 18, active_color=theme.TEXT))
            b.setIconSize(QSize(18, 18))
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._go(idx))
            self.nav_group.addButton(b, i)
            self.nav_buttons.append(b)
            sv.addWidget(b)
        sv.addStretch(1)
        self.chip_usb = label("", "chip")
        self.chip_icloud = label("", "chip")
        self.chip_net = label("", "chip")
        for c in (self.chip_usb, self.chip_icloud, self.chip_net):
            c.setWordWrap(True)
            sv.addWidget(c)
        h.addWidget(side)

        # ---------------- páginas
        self.stack = QStackedWidget()
        self.stack.addWidget(scrollable(self._build_home()))
        self.stack.addWidget(self._build_library())
        self.stack.addWidget(self._build_pending())
        self.stack.addWidget(scrollable(self._build_cleanup()))
        self.stack.addWidget(scrollable(self._build_health()))
        self.stack.addWidget(self._build_alerts())
        self.stack.addWidget(scrollable(self._build_settings()))
        self.stack.addWidget(self._build_log())
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)

    def _go(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.nav_buttons[idx].setChecked(True)
        if idx == P_HEALTH:
            self._refresh_health()
        elif idx == P_ALERTS:
            QTimer.singleShot(1500, self.alerts.mark_all_read)

    # ------------------------------------------------------------------ Inicio
    def _build_home(self) -> QWidget:
        w, lay = page("Tus fotos, en tu compu.", "Completas, verificadas y sin cortes. Por cable o desde iCloud: "
                                              "nada pasa por servidores de terceros.")
        self.banner = label("", "banner", wrap=True)
        self.banner.hide()
        lay.addWidget(self.banner)

        # --- tarjeta en vivo
        live, lv = card()
        top = QHBoxLayout()
        top.addWidget(label("cloudy", "muted"))
        top.addStretch(1)
        self.live_label = label("● Inactivo", "liveOff")
        top.addWidget(self.live_label)
        lv.addLayout(top)
        self.big_title = label("Conectá tu celular o iCloud para empezar", "big", wrap=True)
        lv.addWidget(self.big_title)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        lv.addWidget(self.bar)
        self.line1 = label("", "small")
        self.line2 = label("", "small")
        lv.addWidget(self.line1)
        lv.addWidget(self.line2)

        inner = QFrame()
        inner.setObjectName("inner")
        grid = QGridLayout(inner)
        grid.setContentsMargins(14, 8, 14, 8)
        grid.setVerticalSpacing(0)
        self.stat_values: dict[str, QLabel] = {}
        stats = ["Ahora", "Velocidad", "Último escaneo del celular", "Último listado de iCloud", "Pendientes",
                 "Errores"]
        for r, name in enumerate(stats):
            k = label(name, "muted")
            v = label("—", "value")
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            k.setMinimumHeight(34)
            grid.addWidget(k, r * 2, 0)
            grid.addWidget(v, r * 2, 1)
            if r < len(stats) - 1:
                sep = QFrame()
                sep.setObjectName("sep")
                grid.addWidget(sep, r * 2 + 1, 0, 1, 2)
            self.stat_values[name] = v
        lv.addWidget(inner)

        btns = QHBoxLayout()
        self.btn_main = button("Copiar ahora", "primary", self._main_action)
        self.btn_main.setMinimumHeight(38)
        self.btn_cancel = button("Cancelar", None, self._cancel)
        self.btn_open = button("Abrir carpeta", None, self._open_destination)
        btns.addWidget(self.btn_main, 2)
        btns.addWidget(self.btn_cancel, 1)
        btns.addWidget(self.btn_open, 1)
        lv.addLayout(btns)
        lay.addWidget(live)

        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        self.tile_lib = Tile("photos", "En tu compu", lambda: self._go(P_LIBRARY))
        self.tile_health = Tile("shield", "Salud de la copia", lambda: self._go(P_HEALTH))
        self.tile_disk = Tile("disk", "Disco de destino", lambda: self._go(P_HEALTH))
        self.tile_alerts = Tile("bell", "Alertas", lambda: self._go(P_ALERTS))
        for t in (self.tile_lib, self.tile_health, self.tile_disk, self.tile_alerts):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        # --- fuentes
        row = QHBoxLayout()
        row.setSpacing(14)
        usb, uv = card("Celular por cable", "phone")
        self.usb_status = label("No conectado", "muted", wrap=True)
        uv.addWidget(self.usb_status)
        ub = QHBoxLayout()
        self.btn_usb = button("Conectar celular", "primary", self._toggle_usb)
        self.btn_rescan = button("Volver a escanear", None, lambda: self._scan_usb())
        self.btn_folder_src = button("Tarjeta SD o carpeta…", None, self._connect_folder)
        self.btn_folder_src.setToolTip("Tarjetas de memoria, cámaras de fotos, pendrives, discos o cualquier "
                                       "carpeta con fotos. Solo se lee: no se borra nada.")
        ub.addWidget(self.btn_usb)
        ub.addWidget(self.btn_rescan)
        ub.addWidget(self.btn_folder_src)
        ub.addStretch(1)
        uv.addLayout(ub)
        uv.addWidget(label("iPhone, Samsung y cualquier Android. Copia los originales directo del teléfono. "
                           "Sin internet.", "small", wrap=True))
        row.addWidget(usb, 1)

        icl, iv = card("iCloud Fotos (opcional)", "cloud")
        self.icloud_status = label("Sin sesión", "muted", wrap=True)
        iv.addWidget(self.icloud_status)
        ib = QHBoxLayout()
        self.btn_icloud = button("Conectar iCloud", None, self._toggle_icloud)
        self.btn_relist = button("Volver a listar", None, lambda: self._list_icloud())
        ib.addWidget(self.btn_icloud)
        ib.addWidget(self.btn_relist)
        ib.addStretch(1)
        iv.addLayout(ib)
        iv.addWidget(label("Baja los originales que en el iPhone están optimizados y lo que solo está en la "
                           "nube. Habilita «Liberar espacio».", "small", wrap=True))
        row.addWidget(icl, 1)
        lay.addLayout(row)

        dest, dv = card("Carpeta destino", "folder")
        drow = QHBoxLayout()
        self.dest_label = label("Sin elegir", "value", wrap=True)
        drow.addWidget(self.dest_label, 1)
        drow.addWidget(button("Cambiar…", None, self._choose_destination))
        dv.addLayout(drow)
        self.mirror_summary = label("", "small", wrap=True)
        dv.addWidget(self.mirror_summary)
        lay.addWidget(dest)
        lay.addStretch(1)
        return w

    # ---------------------------------------------------------------- Fototeca
    def _build_library(self) -> QWidget:
        w, lay = page("Fototeca", "Todo lo que hay en tu iPhone y en iCloud, y qué pasa con cada elemento. "
                                  "Clic derecho para más opciones.")
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre o álbum…")
        self.search.textChanged.connect(self._apply_library_filter)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(list(FILTERS.keys()))
        self.filter_combo.currentIndexChanged.connect(self._apply_library_filter)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Todos", "Celular", "iCloud"])
        self.source_combo.currentIndexChanged.connect(self._apply_library_filter)
        bar.addWidget(self.search, 2)
        bar.addWidget(label("Mostrar:", "muted"))
        bar.addWidget(self.filter_combo)
        bar.addWidget(label("Origen:", "muted"))
        bar.addWidget(self.source_combo)
        lay.addLayout(bar)

        self.lib_model = LibraryModel(self)
        self.lib_proxy = LibraryProxy(self)
        self.lib_proxy.setSourceModel(self.lib_model)
        self.lib_view = QTableView()
        self.lib_view.setModel(self.lib_proxy)
        self._style_table(self.lib_view)
        self.lib_view.setSortingEnabled(True)
        self.lib_view.sortByColumn(2, Qt.DescendingOrder)
        self.lib_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lib_view.customContextMenuRequested.connect(self._library_menu)
        self.lib_view.doubleClicked.connect(lambda idx: self._open_row(self._row_at(idx)))
        lay.addWidget(self.lib_view, 1)
        self.lib_count = label("", "small")
        lay.addWidget(self.lib_count)
        return w

    @staticmethod
    def _style_table(v):
        v.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.setAlternatingRowColors(True)
        v.verticalHeader().setVisible(False)
        v.verticalHeader().setDefaultSectionSize(34)
        v.setShowGrid(False)
        hh = v.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setHighlightSections(False)

    # -------------------------------------------------------------- Pendientes
    def _build_pending(self) -> QWidget:
        w, lay = page("Pendientes", "Fotos y videos cuyo original NO estaba en alta calidad en el iPhone (solo en "
                                    "iCloud u optimizados). No se copiaron para no guardar versiones de menor "
                                    "calidad. Cuando estén disponibles, se bajan SOLO estos.")
        tips = label("Dos formas de resolverlos:  ① Conectá iCloud y tocá «Bajar desde iCloud» (lo más rápido).  "
                     "② En el iPhone: Ajustes > [tu nombre] > iCloud > Fotos > «Descargar y conservar originales», "
                     "esperá a que termine y tocá «Buscar en el iPhone».", "info", wrap=True)
        lay.addWidget(tips)
        btns = QHBoxLayout()
        self.btn_pend_icloud = button("Bajar desde iCloud", "primary", lambda: self._start_sync(only_pending=True,
                                                                                               sources=["icloud"]))
        self.btn_pend_usb = button("Buscar en el iPhone", None, self._pending_from_usb)
        self.btn_export = button("Exportar lista (CSV)…", None, self._export_pending)
        btns.addWidget(self.btn_pend_icloud)
        btns.addWidget(self.btn_pend_usb)
        btns.addStretch(1)
        btns.addWidget(self.btn_export)
        lay.addLayout(btns)
        self.pending_table = QTableWidget(0, 5)
        self.pending_table.setHorizontalHeaderLabels(["Archivo", "Tipo", "Fecha de captura", "Tamaño original",
                                                      "Motivo"])
        self._style_table(self.pending_table)
        self.pending_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pending_table.customContextMenuRequested.connect(self._pending_menu)
        lay.addWidget(self.pending_table, 1)
        return w

    # ---------------------------------------------------------- Liberar espacio
    def _build_cleanup(self) -> QWidget:
        w, lay = page("Liberar espacio en iCloud", "Manda a «Eliminado recientemente» de iCloud solo lo pesado que "
                                                   "ya está a salvo en esta compu. Se borra de iCloud y de todos tus "
                                                   "dispositivos, pero Apple lo guarda 30 días por si te arrepentís.")
        lay.addWidget(label("Antes de borrar cada elemento se comprueba que la copia local exista, que tenga "
                            "EXACTAMENTE el tamaño del original de iCloud (nunca se borra algo del que solo tengas "
                            "una versión optimizada) y se vuelve a calcular su SHA-256. Si algo no coincide, ese "
                            "elemento no se toca.", "info", wrap=True))
        self.cleanup_need = label("Para usar esta función, conectá iCloud desde Inicio.", "banner", wrap=True)
        lay.addWidget(self.cleanup_need)

        rules = QGroupBox("Qué se considera «pesado»")
        f = QFormLayout(rules)
        self.cl_min = QSpinBox()
        self.cl_min.setRange(1, 100_000)
        self.cl_min.setSuffix(" MB o más")
        self.cl_videos = QCheckBox("Solo videos")
        self.cl_days = QSpinBox()
        self.cl_days.setRange(0, 36_500)
        self.cl_days.setSpecialValueText("Cualquier fecha")
        self.cl_days.setSuffix(" días o más de antigüedad")
        self.cl_batch = QSpinBox()
        self.cl_batch.setRange(1, 5000)
        self.cl_batch.setSuffix(" elementos por tanda")
        f.addRow("Tamaño:", self.cl_min)
        f.addRow("", self.cl_videos)
        f.addRow("Antigüedad:", self.cl_days)
        f.addRow("Tanda:", self.cl_batch)
        for wdg in (self.cl_min, self.cl_days, self.cl_batch):
            wdg.valueChanged.connect(self._save_cleanup_rules)
        self.cl_videos.toggled.connect(self._save_cleanup_rules)
        lay.addWidget(rules)

        b = QHBoxLayout()
        self.btn_find = button("Buscar qué se puede liberar", "primary", self._find_cleanup)
        self.btn_delete = button("Mandar a «Eliminado recientemente»", "danger", self._delete_cleanup)
        b.addWidget(self.btn_find)
        b.addWidget(self.btn_delete)
        b.addStretch(1)
        lay.addLayout(b)
        self.cleanup_summary = label("", "value", wrap=True)
        lay.addWidget(self.cleanup_summary)
        self.cleanup_table = QTableWidget(0, 5)
        self.cleanup_table.setHorizontalHeaderLabels(["Archivo", "Tipo", "Fecha", "Tamaño", "Copia local"])
        self._style_table(self.cleanup_table)
        self.cleanup_table.setMinimumHeight(320)
        lay.addWidget(self.cleanup_table)
        return w

    # ------------------------------------------------------------------ Ajustes
    def _build_settings(self) -> QWidget:
        w, lay = page("Ajustes", "Qué guardar, cómo organizarlo y cómo sincronizar.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)

        g1 = QGroupBox("Qué guardar")
        v1 = QVBoxLayout(g1)
        self.chk_photos = QCheckBox("Fotos")
        self.chk_videos = QCheckBox("Videos")
        self.chk_live = QCheckBox("Video de las Live Photos (.MOV)")
        self.chk_screens = QCheckBox("Capturas de pantalla")
        self.chk_hidden = QCheckBox("Álbum «Oculto»")
        self.chk_edited = QCheckBox("Versión editada (además del original)")
        self.chk_aae = QCheckBox("Archivos de ajustes de edición (.AAE)")
        self.chk_fav = QCheckBox("Solo favoritos")
        self.chk_and_msg = QCheckBox("Android: fotos y videos de WhatsApp y Telegram")
        self.chk_and_msg.setToolTip("Las recibidas. Las que enviaste (carpeta «Sent») no, porque son copias "
                                    "comprimidas de fotos que ya están en tu cámara.")
        self.chk_and_dl = QCheckBox("Android: fotos y videos de «Descargas»")
        for c in (self.chk_photos, self.chk_videos, self.chk_live, self.chk_screens, self.chk_hidden,
                  self.chk_edited, self.chk_aae, self.chk_fav, self.chk_and_msg, self.chk_and_dl):
            v1.addWidget(c)
        grid.addWidget(g1, 0, 0)

        g2 = QGroupBox("Cómo guardarlo")
        f2 = QFormLayout(g2)
        self.cmb_layout = QComboBox()
        for k, text in LAYOUT_LABELS.items():
            self.cmb_layout.addItem(text, k)
        f2.addRow("Organizar:", self.cmb_layout)
        self.chk_json = QCheckBox("Metadatos .json (fecha, álbumes, favorito, SHA-256)")
        self.chk_verify = QCheckBox("Verificar SHA-256 releyendo del disco")
        self.chk_struct = QCheckBox("Verificar estructura (HEIC, MOV, JPEG, PNG…)")
        self.chk_dates = QCheckBox("Fecha de captura como fecha de creación y modificación")
        self.chk_dates.setToolTip("Así la ves en Propiedades del archivo. La fecha de captura, la cámara y la "
                                  "ubicación ya están dentro de cada foto/video (Propiedades > Detalles).")
        self.chk_json.setVisible(False)   # sin .json: los metadatos van dentro de cada archivo
        self.chk_and_verify = QCheckBox("Android con ADB: comprobar el SHA-256 también en el celular")
        self.chk_and_verify.setToolTip("Prueba de punta a punta: el celular calcula la huella del archivo y tiene "
                                       "que coincidir con la de la copia. Tarda un poco más.")
        for c in (self.chk_verify, self.chk_struct, self.chk_dates, self.chk_and_verify):
            f2.addRow(c)
        self.btn_reorg = button("Reordenar lo ya copiado con esta organización", None, self._reorganize)
        self.btn_reorg.setToolTip("Mueve (no copia) los archivos ya guardados a las carpetas nuevas y borra los "
                                  ".json de metadatos que generaba la versión anterior.")
        f2.addRow(self.btn_reorg)
        f2.addRow(label("Compatibilidad con Windows (los originales no se tocan):", "small"))
        self.chk_compat_photos = QCheckBox("Copia JPG de cada foto HEIC")
        self.chk_compat_videos = QCheckBox("Copia MP4 de cada video HEVC (más lento)")
        self.chk_compat_photos.setToolTip(f"Se guardan en la carpeta «{COMPAT_DIR}» dentro del destino.")
        self.chk_compat_videos.setToolTip(f"Se guardan en la carpeta «{COMPAT_DIR}» dentro del destino.")
        f2.addRow(self.chk_compat_photos)
        f2.addRow(self.chk_compat_videos)
        self.btn_compat = button("Crear copias compatibles de lo ya copiado", None, self._convert_existing)
        f2.addRow(self.btn_compat)
        grid.addWidget(g2, 0, 1)

        g3 = QGroupBox("Cantidad y fechas")
        f3 = QFormLayout(g3)
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(0, 10_000_000)
        self.spin_limit.setSpecialValueText("Todos")
        self.spin_limit.setToolTip("Para probar, empezá con 10. «Todos» = sin límite.")
        f3.addRow("Máximo por copia:", self.spin_limit)
        self.cmb_order = QComboBox()
        self.cmb_order.addItem("Más recientes primero", True)
        self.cmb_order.addItem("Más antiguos primero", False)
        f3.addRow("Orden:", self.cmb_order)
        self.chk_dates_filter = QCheckBox("Solo un rango de fechas")
        f3.addRow(self.chk_dates_filter)
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd/MM/yyyy")
        f3.addRow("Desde:", self.date_from)
        f3.addRow("Hasta:", self.date_to)
        grid.addWidget(g3, 1, 0)

        g4 = QGroupBox("Sincronización")
        f4 = QFormLayout(g4)
        self.chk_auto_usb = QCheckBox("Al conectar el celular, copiar lo nuevo automáticamente")
        f4.addRow(self.chk_auto_usb)
        self.spin_icloud_hours = QSpinBox()
        self.spin_icloud_hours.setRange(0, 168)
        self.spin_icloud_hours.setSpecialValueText("Nunca")
        self.spin_icloud_hours.setSuffix(" h")
        f4.addRow("Revisar iCloud cada:", self.spin_icloud_hours)
        self.chk_phone_data = QCheckBox("Permitir iCloud aunque la compu use los datos móviles del celular "
                                        "(gasta datos)")
        self.chk_phone_data.setToolTip("Apagado (recomendado): si la compu sale a internet por el Punto de acceso "
                                       "del iPhone, iCloud se bloquea. La copia por cable nunca usa datos.")
        f4.addRow(self.chk_phone_data)
        self.chk_demo = QCheckBox("Modo demo (iPhone, Samsung e iCloud simulados)")
        f4.addRow(self.chk_demo)
        self.btn_sim = button("Demo: simular originales descargados", None, self._simulate_download)
        self.btn_unplug = button("Demo: simular corte de cable", None, self._simulate_unplug)
        self.btn_reset_demo = button("Demo: reiniciar", None, self._reset_demo)
        f4.addRow(self.btn_sim)
        f4.addRow(self.btn_unplug)
        f4.addRow(self.btn_reset_demo)
        grid.addWidget(g4, 1, 1)
        lay.addLayout(grid)

        g5 = QGroupBox("Copias espejo (opcional)")
        v5 = QVBoxLayout(g5)
        v5.addWidget(label("Además de la carpeta principal, cada archivo verificado se replica (y se verifica de "
                           "nuevo) en estas carpetas: un NAS (\\\\servidor\\fotos), un disco externo, o la carpeta "
                           "de OneDrive / Google Drive / Dropbox.", "small", wrap=True))
        self.mirror_list = QListWidget()
        self.mirror_list.setMaximumHeight(110)
        v5.addWidget(self.mirror_list)
        mb = QHBoxLayout()
        mb.addWidget(button("Agregar carpeta…", None, self._add_mirror))
        mb.addWidget(button("Quitar", None, self._remove_mirror))
        mb.addStretch(1)
        v5.addLayout(mb)
        self.mirror_warning = label("", "banner", wrap=True)
        self.mirror_warning.hide()
        v5.addWidget(self.mirror_warning)
        lay.addWidget(g5)

        g7 = QGroupBox("Avisos y mantenimiento")
        f7 = QFormLayout(g7)
        self.chk_notify = QCheckBox("Avisos de Windows cuando la app no está al frente (copia terminada, errores)")
        f7.addRow(self.chk_notify)
        self.spin_verify_days = QSpinBox()
        self.spin_verify_days.setRange(0, 365)
        self.spin_verify_days.setSpecialValueText("Nunca")
        self.spin_verify_days.setSuffix(" días")
        f7.addRow("Recordarme verificar la copia cada:", self.spin_verify_days)
        f7.addRow(button("Ver la guía de inicio", None, self._show_onboarding))
        lay.addWidget(g7)

        g6 = QGroupBox("Privacidad")
        v6 = QVBoxLayout(g6)
        v6.addWidget(label(
            "• Por cable no se usa internet: la app bloquea toda conexión que no sea local.\n"
            "• Con iCloud conectado, solo se permiten dominios de Apple (icloud.com, apple.com…).\n"
            "• Si la compu sale a internet por el Punto de acceso del iPhone (datos móviles), iCloud se bloquea.\n"
            "• La contraseña de Apple nunca se guarda. «Cerrar sesión» borra la sesión de esta compu.\n"
            "• Nada se borra del iPhone. Solo «Liberar espacio» manda a «Eliminado recientemente» de iCloud, "
            "y únicamente lo verificado.", "small", wrap=True))
        lay.addWidget(g6)
        lay.addStretch(1)

        for c in (self.chk_photos, self.chk_videos, self.chk_live, self.chk_screens, self.chk_hidden,
                  self.chk_edited, self.chk_aae, self.chk_fav, self.chk_json, self.chk_verify, self.chk_struct,
                  self.chk_dates, self.chk_dates_filter, self.chk_auto_usb, self.chk_phone_data,
                  self.chk_compat_photos, self.chk_compat_videos, self.chk_and_msg, self.chk_and_dl,
                  self.chk_and_verify):
            c.toggled.connect(self._on_settings_changed)
        self.cmb_layout.currentIndexChanged.connect(self._on_settings_changed)
        self.cmb_order.currentIndexChanged.connect(self._on_settings_changed)
        self.spin_limit.valueChanged.connect(self._on_settings_changed)
        self.spin_icloud_hours.valueChanged.connect(self._on_settings_changed)
        self.chk_notify.toggled.connect(self._on_settings_changed)
        self.spin_verify_days.valueChanged.connect(self._on_settings_changed)
        self.date_from.dateChanged.connect(self._on_settings_changed)
        self.date_to.dateChanged.connect(self._on_settings_changed)
        self.chk_dates_filter.toggled.connect(lambda on: (self.date_from.setEnabled(on), self.date_to.setEnabled(on)))
        self.chk_demo.toggled.connect(self._on_demo_toggled)
        return w

    # ----------------------------------------------------------------- Registro
    def _build_log(self) -> QWidget:
        w, lay = page("Registro", "Todo lo que hizo la app, archivo por archivo (con su SHA-256).")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas" if sys.platform == "win32" else "Menlo", 10))
        lay.addWidget(self.log_view, 1)
        b = QHBoxLayout()
        b.addWidget(label("También se guarda en disco, sin correos ni códigos, en la configuración de la app.",
                          "small"))
        b.addStretch(1)
        b.addWidget(button("Abrir carpeta de registros", None, lambda: open_path(logs_dir())))
        b.addWidget(button("Exportar registros…", None, self._export_logs))
        b.addWidget(button("Copiar registro", None,
                           lambda: QGuiApplication.clipboard().setText(self.log_view.toPlainText())))
        lay.addLayout(b)
        return w

    def _export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exportar registros",
                                              str(Path.home() / f"iphone-vault-registros-{datetime.now():%Y%m%d}.zip"),
                                              "ZIP (*.zip)")
        if not path:
            return
        try:
            export_logs(Path(path))
            self.notify("success", "Registros exportados", Path(path).name, alert=False)
        except OSError as e:
            dlg.error(self, "No pude exportar los registros", str(e))

    # ============================================================== ajustes
    def _load_settings_into_ui(self):
        s, o = self.settings, self.settings.options
        widgets = [self.chk_photos, self.chk_videos, self.chk_live, self.chk_screens, self.chk_hidden,
                   self.chk_edited, self.chk_aae, self.chk_fav, self.chk_json, self.chk_verify, self.chk_struct,
                   self.chk_dates, self.chk_dates_filter, self.cmb_layout, self.cmb_order, self.spin_limit,
                   self.date_from, self.date_to, self.chk_demo, self.chk_auto_usb, self.chk_phone_data,
                   self.chk_compat_photos, self.chk_compat_videos, self.chk_and_msg, self.chk_and_dl,
                   self.chk_and_verify, self.spin_icloud_hours, self.chk_notify, self.spin_verify_days,
                   self.cl_min, self.cl_videos, self.cl_days, self.cl_batch]
        for wdg in widgets:
            wdg.blockSignals(True)
        self.chk_photos.setChecked(o.include_photos)
        self.chk_videos.setChecked(o.include_videos)
        self.chk_live.setChecked(o.include_live_videos)
        self.chk_screens.setChecked(o.include_screenshots)
        self.chk_hidden.setChecked(o.include_hidden)
        self.chk_edited.setChecked(o.include_edited)
        self.chk_aae.setChecked(o.include_aae)
        self.chk_fav.setChecked(o.only_favorites)
        self.chk_json.setChecked(False)
        self.chk_verify.setChecked(o.verify_reread)
        self.chk_struct.setChecked(o.verify_structure)
        self.chk_dates.setChecked(o.preserve_dates)
        self.chk_compat_photos.setChecked(o.compat_photos)
        self.chk_compat_videos.setChecked(o.compat_videos)
        self.chk_and_msg.setChecked(o.android_messaging)
        self.chk_and_dl.setChecked(o.android_downloads)
        self.chk_and_verify.setChecked(o.verify_on_device)
        self.cmb_layout.setCurrentIndex(max(0, self.cmb_layout.findData(o.layout)))
        self.cmb_order.setCurrentIndex(0 if o.newest_first else 1)
        self.spin_limit.setValue(o.limit)
        has_dates = bool(o.date_from or o.date_to)
        self.chk_dates_filter.setChecked(has_dates)
        self.date_from.setDate(QDate.fromString(o.date_from, "yyyy-MM-dd") if o.date_from else QDate(2007, 1, 1))
        self.date_to.setDate(QDate.fromString(o.date_to, "yyyy-MM-dd") if o.date_to else QDate.currentDate())
        self.date_from.setEnabled(has_dates)
        self.date_to.setEnabled(has_dates)
        self.chk_demo.setChecked(s.demo_mode)
        self.chk_auto_usb.setChecked(s.auto_sync_usb)
        self.chk_notify.setChecked(s.notify_windows)
        self.spin_verify_days.setValue(s.verify_reminder_days)
        self.chk_phone_data.setChecked(s.icloud_allow_phone_data)
        no_network.allow_phone_data(s.icloud_allow_phone_data)
        self.spin_icloud_hours.setValue(s.icloud_auto_hours)
        self.cl_min.setValue(s.cleanup.min_size_mb)
        self.cl_videos.setChecked(s.cleanup.only_videos)
        self.cl_days.setValue(s.cleanup.older_than_days)
        self.cl_batch.setValue(s.cleanup.batch_size)
        self.mirror_list.clear()
        self.mirror_list.addItems(o.mirror_destinations)
        for wdg in widgets:
            wdg.blockSignals(False)
        self._update_demo_buttons()
        self._update_mirror_warning()

    def _options(self) -> TransferOptions:
        filt = self.chk_dates_filter.isChecked()
        return TransferOptions(
            include_photos=self.chk_photos.isChecked(), include_videos=self.chk_videos.isChecked(),
            include_live_videos=self.chk_live.isChecked(), include_screenshots=self.chk_screens.isChecked(),
            include_hidden=self.chk_hidden.isChecked(), include_edited=self.chk_edited.isChecked(),
            include_aae=self.chk_aae.isChecked(), only_favorites=self.chk_fav.isChecked(),
            write_sidecar_json=False, verify_reread=self.chk_verify.isChecked(),
            verify_structure=self.chk_struct.isChecked(), preserve_dates=self.chk_dates.isChecked(),
            compat_photos=self.chk_compat_photos.isChecked(), compat_videos=self.chk_compat_videos.isChecked(),
            layout=self.cmb_layout.currentData(), limit=self.spin_limit.value(),
            newest_first=bool(self.cmb_order.currentData()),
            date_from=self.date_from.date().toString("yyyy-MM-dd") if filt else None,
            date_to=self.date_to.date().toString("yyyy-MM-dd") if filt else None,
            mirror_destinations=[self.mirror_list.item(i).text() for i in range(self.mirror_list.count())],
            android_messaging=self.chk_and_msg.isChecked(), android_downloads=self.chk_and_dl.isChecked(),
            verify_on_device=self.chk_and_verify.isChecked(),
        )

    def _on_settings_changed(self, *_):
        self.settings.options = self._options()
        self.settings.auto_sync_usb = self.chk_auto_usb.isChecked()
        self.settings.icloud_allow_phone_data = self.chk_phone_data.isChecked()
        no_network.allow_phone_data(self.settings.icloud_allow_phone_data)
        self.settings.icloud_auto_hours = self.spin_icloud_hours.value()
        self.settings.notify_windows = self.chk_notify.isChecked()
        self.settings.verify_reminder_days = self.spin_verify_days.value()
        self._save()
        self._refresh_timer.start()

    def _save_cleanup_rules(self, *_):
        c = self.settings.cleanup
        c.min_size_mb, c.only_videos = self.cl_min.value(), self.cl_videos.isChecked()
        c.older_than_days, c.batch_size = self.cl_days.value(), self.cl_batch.value()
        self._save()

    def _save(self):
        try:
            save_settings(self.settings)
        except OSError as e:
            self._log(f"No pude guardar la configuración: {e}")

    def _add_mirror(self):
        path = QFileDialog.getExistingDirectory(self, "Carpeta para la copia espejo")
        if not path:
            return
        if self.settings.destination and Path(path).resolve() == Path(self.settings.destination).resolve():
            dlg.warn(self, "Esa ya es la carpeta principal", "Elegí otra carpeta (un disco externo o un NAS) para la copia espejo.")
            return
        self.mirror_list.addItem(path)
        self._update_mirror_warning()
        self._on_settings_changed()

    def _remove_mirror(self):
        for it in self.mirror_list.selectedItems():
            self.mirror_list.takeItem(self.mirror_list.row(it))
        self._update_mirror_warning()
        self._on_settings_changed()

    def _update_mirror_warning(self):
        paths = [self.mirror_list.item(i).text() for i in range(self.mirror_list.count())]
        cloud = [p for p in paths if is_cloud_folder(p)]
        if cloud:
            self.mirror_warning.setText("⚠ Estas carpetas parecen sincronizarse con una nube (OneDrive / Google Drive "
                                        "/ Dropbox…): lo que copies ahí SE SUBE a ese servicio. "
                                        + " · ".join(cloud))
            self.mirror_warning.show()
        else:
            self.mirror_warning.hide()
        if hasattr(self, "mirror_summary"):
            self.mirror_summary.setText(("Copias espejo: " + " · ".join(paths)) if paths else
                                        "Sin copias espejo (podés agregar un NAS o disco externo en Ajustes).")

    # ============================================================== destino
    def _choose_destination(self):
        start = self.settings.destination or str(Path.home() / "Pictures")
        path = QFileDialog.getExistingDirectory(self, "Elegí la carpeta donde guardar todo", start)
        if not path:
            return
        if not self._accept_destination(path):
            return
        self.settings.destination = path
        self._save()
        self._open_manifest()
        if self.manifest and self.usb_items:
            register_unavailable(self.usb_items, self.manifest)
        self._refresh_all()

    def _accept_destination(self, path: str) -> bool:
        if is_cloud_folder(path) and not dlg.ask(
                self, "Esa carpeta se sube a una nube",
                "Parece una carpeta de OneDrive, Google Drive o Dropbox: todo lo que guardes ahí se va a subir a "
                "ese servicio. Si querés que las fotos queden solo en tu compu, elegí otra.",
                yes="Usarla igual", no="Elegir otra", kind="warn"):
            return False
        di = disk_info(path)
        if di and di.is_fat and not dlg.ask(
                self, "Ese disco es FAT32",
                "FAT32 no acepta archivos de más de 4 GB: los videos largos (sobre todo en 4K) no se van a poder "
                "copiar ahí. Un disco en NTFS o exFAT no tiene ese límite.",
                yes="Usarlo igual", no="Elegir otro", kind="warn"):
            return False
        return True

    def _pick_destination_for_onboarding(self) -> Optional[str]:
        start = self.settings.destination or str(Path.home() / "Pictures")
        path = QFileDialog.getExistingDirectory(self, "Elegí la carpeta donde guardar todo", start)
        if not path or not self._accept_destination(path):
            return None
        self.settings.destination = path
        self._save()
        self._open_manifest()
        self._refresh_all()
        return path

    def _open_manifest(self):
        if self.manifest:
            self.manifest.close()
            self.manifest = None
        dest = self.settings.destination
        if dest and Path(dest).is_dir():
            try:
                self.manifest = Manifest(Path(dest))
            except Exception as e:  # noqa: BLE001
                dlg.error(self, "No pude usar esa carpeta", str(e))
            else:
                if self.manifest.restored_from is not None:
                    self.notify("warn", "Catálogo recuperado",
                                "El catálogo de la copia estaba dañado y se restauró el respaldo del "
                                f"{self.manifest.restored_from.stem[9:]}. Conviene «Verificar todo» en Salud.",
                                action=("Ir a Salud", lambda: self._go(P_HEALTH)))
        self.dest_label.setText(dest or "Sin elegir — tocá «Cambiar…»")
        self._check_interrupted_job()

    def _check_interrupted_job(self):
        if not self.manifest:
            return
        state = self.manifest.get_meta("job_state")
        parts = list(self.manifest.root.rglob("*.part"))
        if state in ("running", "interrupted", "cancelled") and parts:
            self._interrupted_note = (f"La última copia quedó a mitad ({len(parts)} archivo(s) parcial(es)). "
                                      "Conectá la fuente y tocá «Copiar ahora»: sigue exactamente donde quedó.")
        else:
            self._interrupted_note = ""

    def _open_destination(self):
        if self.settings.destination:
            open_path(Path(self.settings.destination))

    # ============================================================== demo
    def _on_demo_toggled(self, on: bool):
        if self.syncing:
            return
        self._disconnect_usb()
        self._disconnect_icloud(forget=False)
        self.settings.demo_mode = on
        self._save()
        self._update_demo_buttons()
        self._refresh_all()

    def _update_demo_buttons(self):
        demo = self.chk_demo.isChecked()
        for b in (self.btn_sim, self.btn_unplug, self.btn_reset_demo):
            b.setVisible(demo)

    def _simulate_download(self):
        from ...device.demo import build_demo_library, simulate_originals_downloaded
        build_demo_library()
        n = simulate_originals_downloaded()
        self._log(f"[DEMO] El iPhone simulado ahora tiene {n} originales que antes estaban solo en iCloud.")
        dlg.info(self, f"{n} originales «descargados»", "En el iPhone simulado. Ahora probá «Buscar en el iPhone» "
                                                         "en Pendientes.")

    def _simulate_unplug(self):
        if not (self.usb and self.usb.is_demo):
            dlg.info(self, "Falta el celular", "Conectá un celular simulado primero.")
            return
        self.usb.simulate_unplug(after_bytes=2_000_000, replug_after_seconds=6)
        dlg.info(self, "Corte de cable programado", "Durante la próxima copia el cable se «desconecta» y a los 6 "
                                                    "segundos vuelve. Mirá cómo sigue donde quedó.")

    def _reset_demo(self):
        from ...device.android_demo import build_demo_android
        from ...device.demo import build_demo_library
        self._disconnect_usb()
        self._disconnect_icloud(forget=False)
        build_demo_library(reset=True)
        build_demo_android(reset=True)
        self._log("[DEMO] iPhone, Samsung e iCloud simulados reiniciados.")
        dlg.success(self, "Demo reiniciada", "Para repetir desde cero, elegí también una carpeta destino vacía.")
        self._refresh_all()

    # ============================================================== celular / carpeta
    def _toggle_usb(self):
        if self.usb:
            self._disconnect_usb()
            self._refresh_all()
        else:
            self._connect_usb()

    def _connect_usb(self, auto: bool = False, backend: Optional[DeviceBackend] = None):
        """Conecta el celular. Sin `backend`, primero detecta qué hay enchufado (iPhone, Android, cámara)."""
        if backend is None:
            if self.chk_demo.isChecked():
                key = "iphone" if auto else dlg.choose(
                    self, "¿Qué celular simulado?", "El modo demo trae un iPhone y un Samsung de prueba, con fotos "
                    "y videos de ejemplo.", [("iphone", "iPhone"), ("android", "Samsung (Android)")])
                if key is None:
                    return
                if key == "android":
                    from ...device.android_demo import DemoAndroidBackend
                    backend = DemoAndroidBackend()
                else:
                    from ...device.demo import DemoBackend
                    backend = DemoBackend()
            else:
                probe = self._get_probe()
                self._run(lambda t: detect_devices(probe), lambda found: QTimer.singleShot(
                    0, lambda: self._on_detected(found, auto)), None, "Buscando celulares conectados…", quiet=auto)
                return

        opt = self._options()

        def job(t: TaskThread):
            info = backend.connect(on_status=t.status.emit)
            res = scan_backend(backend, progress=lambda m, d, tot: t.status.emit(m), options=opt)
            return info, res

        def ok(result):
            info, res = result
            self.usb = backend
            self.usb_label = info.label
            self._log(f"Conectado a {info.label} por {info.connection}")
            self._apply_usb_scan(res)
            if auto or self.settings.auto_sync_usb:
                self._start_sync(auto=True)

        def fail(_msg):
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass

        self._run(job, ok, fail, "Conectando con el celular…", quiet=auto)

    def _get_probe(self):
        if self._probe is None:
            try:
                from ...device.iphone import IPhoneBackend
                self._probe = IPhoneBackend()
            except Exception:  # noqa: BLE001
                self._probe = None
        return self._probe

    def _on_detected(self, found, auto: bool):
        ready = [f for f in found if f.ready]
        if not ready:
            if auto:
                return
            if any(f.kind == "adb-unauthorized" for f in found):
                dlg.info(self, "Falta un permiso en el celular", "Mirá la pantalla del celular y aceptá "
                         "«¿Permitir depuración USB?» (tildá «Permitir siempre desde esta computadora»). Después "
                         "tocá «Conectar celular» de nuevo.")
                return
            self._connect_help()
            return
        chosen = ready[0]
        if len(ready) > 1:
            if auto:
                chosen = next((f for f in ready if f.kind == "iphone"), ready[0])
            else:
                key = dlg.choose(self, "¿Cuál querés copiar?", "Hay más de un aparato conectado por cable.",
                                 [(str(i), f"{f.label}") for i, f in enumerate(ready[:4])])
                if key is None:
                    return
                chosen = ready[int(key)]
        if auto:
            self._log(f"{chosen.label} detectado: conectando y copiando lo nuevo…")
        self._connect_usb(auto=auto, backend=chosen.make())

    def _connect_help(self):
        mac = sys.platform == "darwin"
        steps = ("<b>iPhone:</b> enchufalo, desbloquealo y tocá «Confiar en esta computadora».<br><br>"
                 "<b>Samsung y otros Android:</b> enchufalo, desbloquealo, bajá la barra de notificaciones, tocá "
                 "el aviso «USB» y elegí <b>«Transferencia de archivos»</b>.")
        if mac or sys.platform.startswith("linux"):
            steps += ("<br><br>En Mac, Android se copia con las herramientas oficiales de Google (ADB): activá "
                      "<b>Opciones de desarrollador › Depuración USB</b> en el celular y aceptá el permiso que "
                      "aparece al enchufarlo.")
        opts = [("retry", "Probar de nuevo"), ("folder", "Tarjeta SD o carpeta…")]
        from ...device.adb import find_adb
        if find_adb() is None and (mac or sys.platform == "win32"):
            opts.append(("adb", "Instalar ADB (opcional)" if not mac else "Instalar ADB"))
        key = dlg.choose(self, "No encuentro ningún celular", steps, opts, kind="info", cancel="Cerrar")
        if key == "retry":
            self._connect_usb()
        elif key == "folder":
            self._connect_folder()
        elif key == "adb":
            self._install_adb()

    def _install_adb(self):
        """Instala las herramientas oficiales de Google con el instalador del sistema, previo consentimiento."""
        if sys.platform == "win32":
            exe, args, src = shutil.which("winget"), ["install", "-e", "--id", "Google.PlatformTools",
                                                       "--accept-source-agreements", "--accept-package-agreements"], \
                "winget (Microsoft)"
        else:
            exe = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                               if Path(p).exists()), None)
            args, src = ["install", "--cask", "android-platform-tools"], "Homebrew"
        if not exe:
            dlg.info(self, "Instalación manual", "Bajá «SDK Platform-Tools» de developer.android.com/tools/releases/"
                     f"platform-tools, descomprimilo y copiá la carpeta «platform-tools» en:\n{config_dir()}\n\n"
                     "Cloudy la encuentra sola.")
            return
        if not dlg.ask(self, "¿Instalar ADB?", f"Se descarga UNA vez, con {src}, el paquete oficial de Google "
                       "«platform-tools». Usa la conexión a internet de tu compu (no la de Cloudy, que sigue "
                       "bloqueada) y no envía nada tuyo.", yes="Instalar"):
            return

        def job(t: TaskThread):
            r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=900,  # nosec B603
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), errors="replace")
            from ...device.adb import find_adb
            if find_adb() is None:
                raise RuntimeError("No se pudo instalar ADB: " + (r.stderr or r.stdout)[-400:])
            return True

        self._run(job, lambda _r: dlg.success(self, "ADB instalado", "Ahora conectá el celular con «Depuración USB» "
                                              "activada."), None, "Instalando ADB…")

    def _connect_folder(self):
        if self.syncing:
            return
        path = QFileDialog.getExistingDirectory(self, "Elegí la tarjeta SD, la cámara o la carpeta con fotos")
        if not path:
            return
        dest = self.settings.destination
        if dest:
            try:
                src_r, dst_r = Path(path).resolve(), Path(dest).resolve()
                if src_r == dst_r or dst_r in src_r.parents or src_r in dst_r.parents:
                    dlg.warn(self, "Esa carpeta no", "El origen no puede ser la carpeta destino (ni estar dentro "
                             "de ella, ni contenerla).")
                    return
            except OSError:
                pass
        from ...device.folder import FolderBackend
        self._disconnect_usb()
        self._connect_usb(backend=FolderBackend(path, exclude=[dest] if dest else None))

    def _scan_usb(self, then: Optional[Callable] = None):
        if not self.usb:
            return
        backend = self.usb
        opt = self._options()

        def job(t: TaskThread):
            return scan_backend(backend, progress=lambda m, d, tot: t.status.emit(m), options=opt)

        def ok(res):
            self._apply_usb_scan(res)
            if then:
                then()

        self._run(job, ok, None, "Escaneando el celular…")

    def _apply_usb_scan(self, res):
        self.usb_items = res.items
        self.usb_warnings = res.warnings
        self.usb_scan_time = datetime.now()
        st = res.stats
        if "items_total" in st:
            self._log(f"Escaneo del iPhone: {st.get('items_total', 0)} elementos · disponibles {st.get('local', 0)} "
                      f"· solo iCloud {st.get('cloud_only', 0)} · optimizados {st.get('optimized', 0)}")
        else:
            self._log(f"Escaneo: {st.get('photos', 0)} fotos y {st.get('videos', 0)} videos")
        for w_ in res.warnings:
            self.notify("warn", "Revisá el celular", w_, toast=False)
        if self.manifest:
            register_unavailable(res.items, self.manifest)
        self._refresh_all()

    def _disconnect_usb(self):
        if self.usb:
            try:
                self.usb.close()
            except Exception:  # noqa: BLE001
                pass
        self.usb = None
        self.usb_items = []
        self.usb_warnings = []

    # ============================================================== iCloud
    def _toggle_icloud(self):
        if self.icloud:
            if dlg.ask(self, "¿Cerrar la sesión de iCloud?", "Se borra la sesión guardada en esta compu. Para volver a "
                       "conectar vas a necesitar la contraseña y el código de Apple.", yes="Cerrar sesión"):
                self._disconnect_icloud(forget=True)
                self._refresh_all()
        else:
            self._login_icloud()

    def _login_icloud(self):
        demo = self.chk_demo.isChecked()
        d = ICloudLoginDialog(self, self.settings.icloud_apple_id, demo)
        if d.run() != "yes":
            return
        apple_id, password = d.apple_id.text().strip(), d.password.text()
        if not apple_id and not demo:
            return
        if demo:
            from ...device.icloud_demo import DemoICloudAccount
            account = DemoICloudAccount(apple_id)
        else:
            from ...device.icloud import ICloudAccount
            account = ICloudAccount(apple_id)

        def job(t: TaskThread):
            return account.login(password)

        def ok(state):
            self.settings.icloud_apple_id = apple_id
            self.settings.icloud_enabled = True
            self._save()
            if state == "2fa":
                self._ask_2fa(account)
            else:
                self._icloud_ready(account)

        self._run(job, ok, None, "Iniciando sesión en iCloud…")

    def _ask_2fa(self, account):
        code = dlg.ask_text(self, "Verificación en 2 pasos",
                            "Ingresá el código de 6 dígitos que apareció en tu iPhone o Mac.", ok="Verificar",
                            digits=6)
        if not code:
            return

        def job(t: TaskThread):
            return account.submit_code(code)

        def done(valid):
            if valid:
                self._icloud_ready(account)
            else:
                self.notify("error", "Código incorrecto", "Probá de nuevo con el código más reciente.", alert=False)
                self._ask_2fa(account)

        self._run(job, done, None, "Verificando el código…")

    def _icloud_ready(self, account):
        if account.is_demo:
            from ...device.icloud_demo import DemoICloudBackend
            self.icloud = DemoICloudBackend(account)
        else:
            from ...device.icloud import ICloudBackend
            self.icloud = ICloudBackend(account)
            no_network.allow_icloud(True)
        self.icloud_account = account
        self._log(f"Sesión de iCloud iniciada: {account.apple_id}")
        self._list_icloud()

    def _list_icloud(self, then: Optional[Callable] = None):
        if not self.icloud:
            return
        backend = self.icloud

        def job(t: TaskThread):
            return backend.list_items(progress=t.status.emit)

        def ok(items):
            self.icloud_items = items
            self.icloud_list_time = datetime.now()
            self._log(f"Listado de iCloud: {len(items)} elementos")
            self._refresh_all()
            if then:
                then()

        self._run(job, ok, None, "Listando iCloud Fotos…")

    def _disconnect_icloud(self, forget: bool):
        self.icloud = None
        self.icloud_account = None
        self.icloud_items = []
        no_network.allow_icloud(False)
        if forget:
            from ...device.icloud import forget_session
            forget_session()
            self.settings.icloud_enabled = False
            self._save()
            self._log("Sesión de iCloud cerrada y borrada de esta compu.")

    # ============================================================== plan y estado
    def _compute_plans(self) -> dict[str, Plan]:
        plans: dict[str, Plan] = {}
        if not self.manifest:
            return plans
        opt = self._options()
        if self.usb and self.usb_items:
            plans["usb"] = build_plan(self.usb_items, opt, self.manifest)
        if self.icloud and self.icloud_items:
            o = copy.copy(opt)
            usb_fps = {i.fingerprint for i in plans["usb"].items} if "usb" in plans else set()
            if opt.limit:
                o.limit = max(0, opt.limit - len(plans.get("usb", Plan([])).items))
            if not opt.limit or o.limit > 0:
                p = build_plan([i for i in self.icloud_items if i.fingerprint not in usb_fps], o, self.manifest)
                plans["icloud"] = p
        return plans

    def _classify(self, r: Row, opt, plan_keys: set, excluded: set, failed: set, done_fps: set, deleted: set,
                  files_map: dict) -> str:
        it = r.item
        if it.is_trashed:
            return ST_TRASH
        if it.key in excluded or (r.alt and r.alt.key in excluded):
            return ST_USER_EXCLUDED
        if it.key in plan_keys:
            return ST_WILL
        if r.alt and r.alt.key in plan_keys:
            return ST_WILL_CLOUD
        fp = it.fingerprint
        own_done = all(f.path in files_map for f in wanted_files(it, opt)) and it.files
        if (fp and fp in done_fps) or own_done:
            return ST_DELETED if (it.key in deleted or (r.alt and r.alt.key in deleted)) else ST_DONE
        if it.key in failed:
            return ST_FAILED
        if it.availability == AVAIL_CLOUD_ONLY:
            return ST_PENDING
        if it.availability == AVAIL_OPTIMIZED:
            return ST_OPTIMIZED
        if not passes_filters(it, opt):
            return ST_EXCLUDED
        return ST_LIMIT

    def _refresh_all(self):
        self._reorg_cache = None
        opt = self._options()
        self.plans = self._compute_plans()
        plan_keys = {i.key for p in self.plans.values() for i in p.items}
        self.rows = merge_rows(self.usb_items, self.icloud_items)
        if self.manifest:
            excluded = self.manifest.keys_with_status(STATUS_EXCLUDED)
            failed = self.manifest.keys_with_status(STATUS_FAILED)
            done_fps = self.manifest.done_fingerprints()
            deleted = self.manifest.deleted_keys()
            files_map = self.manifest.files_map()
        else:
            excluded = failed = done_fps = deleted = set()
            files_map = {}
        for r in self.rows:
            r.status = self._classify(r, opt, plan_keys, excluded, failed, done_fps, deleted, files_map)
        self._totals = self._library_totals()
        self._pending_count = len(self.manifest.pending_rows()) if self.manifest else 0
        self.lib_model.set_rows(self.rows)
        self._apply_library_filter()
        self._refresh_pending()
        self._refresh_chips()
        self._refresh_dashboard()
        self._refresh_buttons()

    def _library_totals(self) -> tuple[int, int, int, int]:
        total = done = total_b = done_b = 0
        for r in self.rows:
            if r.status == ST_TRASH:
                continue
            size = max(r.item.best_size, r.alt.best_size if r.alt else 0)
            total += 1
            total_b += size
            if r.status in (ST_DONE, ST_DELETED):
                done += 1
                done_b += size
        return done, total, done_b, total_b

    def _refresh_dashboard(self):
        n_new = sum(len(p.items) for p in self.plans.values())
        b_new = sum(p.total_bytes for p in self.plans.values())
        done, total, done_b, total_b = getattr(self, "_totals", (0, 0, 0, 0))
        p = self.last_progress if self.syncing else None
        sources = bool(self.usb or self.icloud)

        if self.syncing and p:
            if p.state == STATE_PAUSED:
                self.live_label.setText("❚❚ En pausa")
                set_role(self.live_label, "liveOff")
            elif p.state == STATE_WAITING:
                self.live_label.setText("● Esperando conexión")
                set_role(self.live_label, "liveOff")
            else:
                self.live_label.setText("● En vivo")
                set_role(self.live_label, "live")
            remaining = max(0, p.items_total - p.items_done)
            if p.state == STATE_PAUSED:
                title = f"En pausa · quedan {theme.num(remaining)}"
            elif p.state == STATE_WAITING:
                title = f"Esperando conexión · quedan {theme.num(remaining)}"
            elif p.state == STATE_CONVERTING:
                title = "Creando copias compatibles con Windows"
            else:
                title = f"Copiando {theme.num(remaining)} nuevos"
            self.big_title.setText(title + (f" · {self.phase}" if self.phase else ""))
            frac = p.bytes_done / p.bytes_total if p.bytes_total else 0
            self.bar.setValue(int(frac * 1000))
            self.line1.setText(f"{int(frac * 100)} % · {theme.gb_pair(p.bytes_done, p.bytes_total)} · "
                               f"{theme.eta(p.eta_seconds) if p.state == 'copiando' else p.state}")
            if p.state == STATE_CONVERTING:
                self.stat_values["Velocidad"].setText("—")
            self.stat_values["Ahora"].setText(p.current or "—")
            self.stat_values["Velocidad"].setText(f"{theme.size(p.speed_bps)}/s" if p.speed_bps else "—")
            if p.message:
                self.line1.setText(p.message)
        else:
            self.live_label.setText("● Inactivo" if not self.syncing else "● En vivo")
            set_role(self.live_label, "liveOff" if not self.syncing else "live")
            if self.syncing:
                self.big_title.setText("Preparando la copia…")
            elif not sources:
                self.big_title.setText("Conectá tu celular o iCloud para empezar")
            elif not self.manifest:
                self.big_title.setText("Elegí una carpeta destino")
            elif n_new:
                self.big_title.setText(f"Listo para copiar {theme.num(n_new)} nuevos")
            else:
                self.big_title.setText("Todo al día ✓")
            self.bar.setValue(int(done_b / total_b * 1000) if total_b else 0)
            limit = self._options().limit
            self.line1.setText((f"{theme.size(b_new)} por copiar" if n_new else "Nada nuevo por copiar")
                               + (f" · límite de prueba: {limit} por copia" if limit else ""))
            self.stat_values["Ahora"].setText("—")
            self.stat_values["Velocidad"].setText("—")
        if p:
            done += p.items_done
            done_b += p.bytes_done
        self.line2.setText(f"Fototeca: {theme.num(min(done, total))} de {theme.num(total)} elementos en la compu · "
                           f"{theme.gb_pair(min(done_b, total_b), total_b)}" if total else "Fototeca: todavía sin escanear")
        self.stat_values["Último escaneo del celular"].setText(theme.ago(self.usb_scan_time))
        self.stat_values["Último listado de iCloud"].setText(theme.ago(self.icloud_list_time))
        pend = getattr(self, "_pending_count", 0)
        self.stat_values["Pendientes"].setText(theme.num(pend))
        set_role(self.stat_values["Pendientes"], "bad" if pend else "value")
        errors = (p.errors if p else 0) + self.session_errors
        self.stat_values["Errores"].setText(theme.num(errors))
        set_role(self.stat_values["Errores"], "bad" if errors else "value")

        notes = list(self.usb_warnings)
        if self.manifest and not self.syncing:
            if getattr(self, "_reorg_cache", None) is None:
                self._reorg_cache = (count_pending(self.manifest, self._options().layout), count_sidecars(self.manifest))
            moves, sides = self._reorg_cache
            if moves or sides:
                parts = []
                if moves:
                    parts.append(f"{theme.num(moves)} archivos están en carpetas de la organización anterior")
                if sides:
                    parts.append(f"hay {theme.num(sides)} archivos .json de metadatos que ya no se usan")
                notes.append(" y ".join(parts).capitalize() + ". Ajustes > «Reordenar lo ya copiado» lo arregla "
                             "en segundos (solo mueve archivos, no recomprime nada).")
        self._hotspot = hotspot_interfaces()
        if self._hotspot and not self.chk_phone_data.isChecked():
            notes.insert(0, HOTSPOT_MESSAGE + f"  [conexión: {', '.join(self._hotspot)}]")
        if getattr(self, "_interrupted_note", ""):
            notes.insert(0, self._interrupted_note)
        if self.icloud and pend:
            notes = [n for n in notes if "NO tienen el original" not in n]
            notes.append(f"Hay {pend} pendientes y tenés iCloud conectado: «Copiar ahora» los baja desde iCloud "
                         "en calidad original.")
        if notes:
            self.banner.setText("⚠ " + "\n\n⚠ ".join(notes))
            self.banner.show()
        else:
            self.banner.hide()

        self.usb_status.setText(f"● Conectado: {self.usb_label}\n{theme.num(len(self.usb_items))} elementos"
                                if self.usb else "No conectado. Enchufá el cable, desbloqueá el celular y tocá "
                                                 "«Conectar».")
        self.btn_usb.setText("Desconectar" if self.usb else "Conectar celular")
        set_role(self.btn_usb, "" if self.usb else "primary")
        if self.icloud:
            self.icloud_status.setText(f"● Sesión: {self.icloud_account.apple_id}\n"
                                       f"{theme.num(len(self.icloud_items))} elementos en iCloud")
        else:
            self.icloud_status.setText("Sin sesión. Opcional: sirve para los originales optimizados y para "
                                       "liberar espacio.")
        self.btn_icloud.setText("Cerrar sesión" if self.icloud else "Conectar iCloud")
        self.cleanup_need.setVisible(self.icloud is None)
        self._refresh_tiles()
        self._update_mirror_warning()

    def _refresh_tiles(self):
        if not self.manifest:
            for t in (self.tile_lib, self.tile_health, self.tile_disk):
                t.set("—", "Elegí una carpeta destino")
            return
        if getattr(self, "_tiles_cache", None) is None or not self.syncing:
            fm = self.manifest.files_map()
            self._tiles_cache = (len(fm), sum(v[0] for v in fm.values()))
        n, b = self._tiles_cache
        self.tile_lib.set(f"{theme.num(n)} archivos", theme.size(b) + " verificados", "good" if n else "neutral")
        lv = last_verification(self.manifest)
        if lv["at"] is None:
            self.tile_health.set("Sin verificar", "Tocá para comprobar tu copia", "warn" if n else "neutral")
        elif lv["problems"]:
            self.tile_health.set(f"{lv['problems']} problema(s)", f"Verificado {theme.ago(lv['at'])}", "bad")
        else:
            self.tile_health.set("Sana", f"Verificada {theme.ago(lv['at'])}", "good")
        di = disk_info(self.manifest.root)
        if di:
            self.tile_disk.set(f"{theme.size(di.free)} libres", f"{int(di.used_ratio * 100)} % usado"
                               + (" · FAT32" if di.is_fat else ""), "warn" if (di.low_space or di.is_fat) else "good")
        self._refresh_alert_badge()

    def _refresh_chips(self):
        self.chip_usb.setText(f"● Celular: {'conectado' if self.usb else 'no conectado'}")
        self.chip_icloud.setText(f"● iCloud: {'conectado' if self.icloud else 'sin sesión'}")
        if hotspot_interfaces():
            self.chip_net.setText("⚠ Compu usando datos móviles del celular" +
                                  (" (iCloud bloqueado)" if not self.chk_phone_data.isChecked() else ""))
            self.chip_net.setStyleSheet(f"color:{theme.ORANGE};")
        else:
            self.chip_net.setText("🔒 " + no_network.describe())
            self.chip_net.setStyleSheet("")
        self.chip_usb.setStyleSheet(f"color:{theme.GREEN if self.usb else theme.MUTED};")
        self.chip_icloud.setStyleSheet(f"color:{theme.GREEN if self.icloud else theme.MUTED};")
        pend = getattr(self, "_pending_count", 0)
        self.nav_buttons[2].setText(f"  Pendientes ({pend})" if pend else "  Pendientes")

    def _refresh_buttons(self, busy: Optional[bool] = None):
        if busy is None:
            busy = bool(self.task and self.task.isRunning() and not self.task.completed)
        has_plan = any(p.items for p in self.plans.values())
        if self.syncing:
            self.btn_main.setText("Reanudar" if self.pause_event.is_set() else "Pausar")
            self.btn_main.setEnabled(True)
        else:
            self.btn_main.setText("Copiar ahora")
            self.btn_main.setEnabled(not busy and has_plan and self.manifest is not None)
        self.btn_cancel.setEnabled(self.syncing)
        self.btn_open.setEnabled(bool(self.settings.destination))
        self.btn_usb.setEnabled(not busy and not self.syncing)
        self.btn_rescan.setEnabled(not busy and self.usb is not None)
        self.btn_icloud.setEnabled(not busy and not self.syncing)
        self.btn_relist.setEnabled(not busy and self.icloud is not None)
        self.btn_pend_icloud.setEnabled(not busy and self.icloud is not None and self.manifest is not None)
        self.btn_pend_usb.setEnabled(not busy and self.usb is not None and self.manifest is not None)
        self.btn_export.setEnabled(self.manifest is not None)
        self.btn_find.setEnabled(not busy and self.icloud is not None and self.manifest is not None)
        self.btn_reorg.setEnabled(not busy and not self.syncing and self.manifest is not None)
        self.btn_compat.setEnabled(not busy and not self.syncing and self.manifest is not None)
        self.btn_delete.setEnabled(not busy and bool(self.cleanup_candidates))
        self.chk_demo.setEnabled(not busy and not self.syncing)
        if hasattr(self, "btn_verify") and self.stack.currentIndex() == P_HEALTH:
            self._refresh_health()

    # ============================================================== tablas
    def _apply_library_filter(self, *_):
        self.lib_proxy.set_filters(self.search.text(), FILTERS[self.filter_combo.currentText()],
                                   self.source_combo.currentText())
        self.lib_count.setText(f"{theme.num(self.lib_proxy.rowCount())} de {theme.num(len(self.rows))} elementos")

    def _row_at(self, proxy_index) -> Optional[Row]:
        if not proxy_index.isValid():
            return None
        return self.lib_model.rows[self.lib_proxy.mapToSource(proxy_index).row()]

    def _local_file(self, r: Row) -> Optional[Path]:
        if not self.manifest:
            return None
        for it in (r.item, r.alt):
            if it is None:
                continue
            for f in it.files:
                rec = self.manifest.file_record(f.path)
                if rec and (self.manifest.root / rec["local_path"]).exists():
                    return self.manifest.root / rec["local_path"]
        key = self.manifest.done_key_by_fingerprint().get(r.item.fingerprint or "")
        if key:
            for rec in self.manifest.files_of_item(key):
                p = self.manifest.root / rec["local_path"]
                if rec["role"] == "original" and p.exists():
                    return p
        return None

    def _open_row(self, r: Optional[Row]):
        if r and (p := self._local_file(r)):
            open_path(self._viewable(p))

    def _viewable(self, p: Path) -> Path:
        """Si existe la copia compatible (JPG/MP4), se abre esa: Windows no abre HEIC/HEVC sin códecs."""
        if not self.manifest:
            return p
        try:
            rel = p.relative_to(self.manifest.root).as_posix()
        except ValueError:
            return p
        c = compat_rel(rel)
        return self.manifest.root / c if c and (self.manifest.root / c).exists() else p

    def _library_menu(self, pos: QPoint):
        idx = self.lib_view.indexAt(pos)
        r = self._row_at(idx)
        if r is None:
            return
        menu = QMenu(self)
        local = self._local_file(r)
        a_open = menu.addAction("Abrir")
        a_show = menu.addAction("Mostrar en la carpeta")
        a_path = menu.addAction("Copiar ruta local")
        for a in (a_open, a_show, a_path):
            a.setEnabled(local is not None)
        a_name = menu.addAction("Copiar nombre")
        menu.addSeparator()
        a_now = menu.addAction("Copiar solo este ahora")
        a_now.setEnabled(r.status in (ST_WILL, ST_WILL_CLOUD, ST_LIMIT, ST_FAILED, ST_EXCLUDED) and not self.syncing)
        excluded = r.status == ST_USER_EXCLUDED
        a_excl = menu.addAction("Volver a incluir" if excluded else "Excluir de la copia")
        a_excl.setEnabled(self.manifest is not None and r.status != ST_DONE)
        chosen = menu.exec(self.lib_view.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == a_open and local:
            open_path(self._viewable(local))
        elif chosen == a_show and local:
            reveal_in_folder(local)
        elif chosen == a_path and local:
            QGuiApplication.clipboard().setText(str(local))
        elif chosen == a_name:
            QGuiApplication.clipboard().setText(r.item.name)
        elif chosen == a_now:
            self._start_sync(single=r)
        elif chosen == a_excl:
            for it in (r.item, r.alt):
                if it is not None:
                    self.manifest.set_excluded(it, not excluded)
            self._refresh_all()

    def _refresh_pending(self):
        rows = self.manifest.pending_rows() if self.manifest else []
        t = self.pending_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            iso = row["capture_date"] or ""
            date_txt = f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]} {iso[11:16]}" if iso else "—"
            vals = [row["primary_path"].rsplit("/", 1)[-1], "Video" if row["kind"] == "video" else "Foto", date_txt,
                    theme.size(row["expected_size"]), row["reason"] or ""]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                cell.setData(Qt.UserRole, row["key"])
                t.setItem(i, c, cell)
        t.setSortingEnabled(True)

    def _pending_menu(self, pos: QPoint):
        item = self.pending_table.itemAt(pos)
        if item is None or not self.manifest:
            return
        key = item.data(Qt.UserRole)
        name = self.pending_table.item(item.row(), 0).text()
        menu = QMenu(self)
        a_copy = menu.addAction("Copiar nombre")
        a_remove = menu.addAction("Quitar de la lista (no lo quiero)")
        chosen = menu.exec(self.pending_table.viewport().mapToGlobal(pos))
        if chosen == a_copy:
            QGuiApplication.clipboard().setText(name)
        elif chosen == a_remove:
            self.manifest.remove_pending(key)
            self._refresh_all()

    def _export_pending(self):
        if not self.manifest:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exportar pendientes",
                                              str(Path(self.settings.destination) / "pendientes_icloud.csv"),
                                              "CSV (*.csv)")
        if path:
            n = self.manifest.export_pending_csv(Path(path))
            self._log(f"Lista de pendientes exportada ({n} elementos): {path}")

    def _pending_from_usb(self):
        self._scan_usb(then=lambda: self._start_sync(only_pending=True, sources=["usb"]))

    # ============================================================== copiar
    def _main_action(self):
        if self.syncing:
            if self.pause_event.is_set():
                self.pause_event.clear()
                self._log("Reanudado.")
            else:
                self.pause_event.set()
                self._log("En pausa. Lo copiado queda guardado; «Reanudar» sigue desde el mismo byte.")
            self._refresh_buttons()
            return
        self._start_sync()

    def _start_sync(self, auto: bool = False, only_pending: bool = False, sources: Optional[list[str]] = None,
                    single: Optional[Row] = None):
        if not self.manifest:
            dlg.warn(self, "Falta la carpeta destino", "Elegí dónde guardar todo en Inicio > «Cambiar…».")
            return
        if self.syncing or (self.task and self.task.isRunning() and not self.task.completed):
            return
        opt = self._options()
        if only_pending or single:
            opt.limit = 0
        backends = []
        if self.usb and (sources is None or "usb" in sources):
            backends.append(("usb", "el celular", self.usb, self.usb_items))
        if self.icloud and (sources is None or "icloud" in sources):
            backends.append(("icloud", "iCloud", self.icloud, self.icloud_items))
        if single is not None:
            target = single.alt if (single.status == ST_WILL_CLOUD and single.alt) else single.item
            backends = [b for b in backends if b[0] == target.source]
            backends = [(k, n, be, [target]) for k, n, be, _ in backends]
        if not backends:
            dlg.info(self, "No hay ninguna fuente conectada", "Conectá el celular por cable o iniciá sesión en "
                                                               "iCloud primero.")
            return

        manifest = self.manifest
        if only_pending or single:
            preview = sum(len(build_plan(items, opt, manifest, only_pending).items) for *_x, items in backends)
            if not preview:
                dlg.info(self, "No hay nada para copiar todavía",
                         "Si esperabas los pendientes desde el iPhone: asegurate de que haya terminado «Descargar "
                         "y conservar originales»." if only_pending else "")
                return
        elif not auto:
            n = sum(len(p.items) for p in self.plans.values())
            b = sum(p.total_bytes for p in self.plans.values())
            mirrors = f"\nCopias espejo: {', '.join(opt.mirror_destinations)}" if opt.mirror_destinations else ""
            di = disk_info(self.settings.destination)
            extra = ""
            if di and di.is_fat:
                big = too_big_for_fat([f.size for p in self.plans.values() for _it, fs in p.entries for f in fs])
                if big:
                    extra = (f"\n\n⚠ {big} archivo(s) pesan más de 4 GB y el disco es FAT32: esos no se van a "
                             "poder copiar.")
            if di and b > di.free:
                dlg.error(self, "No entra en el disco",
                          f"Hace falta {theme.size(b)} y en el destino quedan {theme.size(di.free)} libres. Liberá "
                          "espacio o elegí otro disco.")
                return
            if not dlg.ask(
                    self, f"¿Copiar {theme.num(n)} elementos ({theme.size(b)})?",
                    f"Destino: {self.settings.destination}{mirrors}\n\nCada archivo se verifica (tamaño exacto, "
                    "SHA-256 y estructura) y la compu no se suspende mientras copia. No se borra nada del iPhone ni "
                    f"de iCloud.{extra}", yes="Copiar ahora"):
                return

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        cancel, pause = self.cancel_event, self.pause_event
        self.syncing = True
        self.last_progress = None
        self.session_errors = 0

        def job(t: TaskThread):
            reports = []
            remaining = opt.limit
            for i, (key, name, backend, items) in enumerate(backends, start=1):
                o = copy.copy(opt)
                if opt.limit:
                    if remaining <= 0:
                        break
                    o.limit = remaining
                plan = build_plan(items, o, manifest, only_pending=only_pending)
                if not plan.items:
                    continue
                t.status.emit(f"Paso {i}/{len(backends)} · desde {name}" if len(backends) > 1 else f"desde {name}")
                eng = TransferEngine(backend, manifest, o, progress=t.progress.emit, log_cb=t.log_line.emit,
                                     cancel_event=cancel, pause_event=pause)
                rep = eng.run(plan)
                reports.append((name, rep))
                remaining -= rep.items_done
                if rep.cancelled or rep.interrupted:
                    break
            return reports

        def ok(reports):
            self.syncing = False
            self.phase = ""
            self._check_interrupted_job()
            if not reports:
                self._refresh_all()
                self.notify("info", "No había nada para copiar", "Todo lo disponible ya está en tu compu.", alert=False)
                return
            lines = []
            failed = 0
            for name, r in reports:
                failed += len(r.failed)
                speed = r.copied_bytes / r.seconds if r.seconds else 0
                extra = []
                if r.resumed_files:
                    extra.append(f"{r.resumed_files} retomados")
                if r.reconnections:
                    extra.append(f"{r.reconnections} reconexiones")
                if r.warnings:
                    extra.append(f"{len(r.warnings)} advertencias")
                if r.compat_created:
                    extra.append(f"{r.compat_created} copias compatibles JPG/MP4")
                if r.compat_failed:
                    extra.append(f"{len(r.compat_failed)} copias compatibles fallidas (ver Registro)")
                lines.append(f"Desde {name}: {r.items_done} elementos, {r.copied_files} archivos "
                             f"({theme.size(r.copied_bytes)}) verificados en {int(r.seconds)} s "
                             f"({theme.size(speed)}/s)" + (f" · {', '.join(extra)}" if extra else ""))
                if r.cancelled:
                    lines.append("Cancelado: lo verificado quedó guardado y lo parcial se retoma la próxima vez.")
                if r.interrupted:
                    lines.append(r.interrupted)
            self.session_errors = failed
            if failed:
                lines.append(f"\n{failed} archivos con error (ver Registro). Tocá «Copiar ahora» para reintentar: "
                             "lo ya copiado no se repite.")
            summary = "\n".join(lines)
            self._log(summary)
            self._refresh_all()
            self._after_sync(reports, failed, summary, auto)

        def fail(_msg):
            self.syncing = False
            self.phase = ""
            self._refresh_all()

        self._run(job, ok, fail, "Preparando la copia…", quiet=auto)

    def _reorganize(self):
        if not self.manifest:
            dlg.warn(self, "Falta la carpeta destino", "Elegí dónde guardar todo en Inicio > «Cambiar…».")
            return
        if self.syncing:
            dlg.info(self, "Hay una copia en curso", "Esperá a que termine para reordenar.")
            return
        layout = self._options().layout
        moves, sides = count_pending(self.manifest, layout), count_sidecars(self.manifest)
        if not moves and not sides:
            self.notify("success", "Ya está todo ordenado", "No hay nada que mover con esta organización.",
                        alert=False)
            return
        if not dlg.ask(
                self, "¿Reordenar lo ya copiado?",
                f"Se van a mover {theme.num(moves)} archivos a «{self.cmb_layout.currentText()}» y borrar "
                f"{theme.num(sides)} archivos .json de metadatos que generaba la app.\n\nNo se copia ni recomprime "
                "nada: la calidad y el SHA-256 no cambian, y la app sigue sabiendo qué ya copió.", yes="Reordenar"):
            return
        manifest, mirrors = self.manifest, self._options().mirror_destinations

        def job(t: TaskThread):
            return reorganize(manifest, layout, True, mirrors,
                              lambda i, n, name: t.status.emit(f"Reordenando {i} de {n}: {name}"))

        def ok(rep):
            msg = (f"Listo: {rep.moved} archivos movidos, {rep.sidecars_removed} .json borrados, "
                   f"{rep.empty_dirs_removed} carpetas vacías eliminadas.")
            if rep.failed:
                msg += f"\n\n{len(rep.failed)} no se pudieron mover (ver Registro)."
                for rel, err in rep.failed:
                    self._log(f"No se pudo mover {rel}: {err}")
            self._log(msg)
            self._refresh_all()
            self.notify("warn" if rep.failed else "success", "Reordenado", msg)

        self._run(job, ok, None, "Reordenando…")

    def _convert_existing(self):
        if not self.manifest:
            dlg.warn(self, "Falta la carpeta destino", "Elegí dónde guardar todo en Inicio > «Cambiar…».")
            return
        photos, videos = self.chk_compat_photos.isChecked(), self.chk_compat_videos.isChecked()
        if not (photos or videos):
            dlg.info(self, "Elegí qué convertir", "Activá al menos una de las dos opciones (JPG o MP4).")
            return
        missing = compat_missing_libs(photos, videos)
        if missing:
            dlg.warn(self, "Faltan componentes para convertir",
                     "Cerrá la app y abrila de nuevo con «Iniciar Cloudy.bat»: los instala solo.",
                     details="Faltan: " + ", ".join(missing))
            return
        manifest = self.manifest
        cancel = threading.Event()
        self.cancel_event = cancel

        def job(t: TaskThread):
            return convert_existing(manifest, videos, lambda i, n, name: t.status.emit(
                f"Creando copias compatibles {i} de {n}: {name}"), cancel, photos=photos)

        def ok(rep):
            msg = (f"Copias compatibles creadas: {rep.created} (ya existían u omitidas: {rep.skipped}).\n"
                   f"Están en la carpeta «{COMPAT_DIR}» dentro del destino.")
            if rep.failed:
                msg += f"\n\n{len(rep.failed)} no se pudieron convertir (ver Registro)."
                for rel, err in rep.failed:
                    self._log(f"No se pudo convertir {rel}: {err}")
            self._log(msg)
            self.notify("warn" if rep.failed else "success", "Copias compatibles listas", msg)

        self._run(job, ok, None, "Creando copias compatibles…")

    def _cancel(self):
        if self.syncing:
            self.pause_event.clear()
            self.cancel_event.set()
            self._log("Cancelando… lo verificado queda guardado y el archivo en curso se retoma la próxima vez.")

    def _on_progress(self, p):
        if isinstance(p, tuple) and p and p[0] == "verify":
            self._on_verify_progress(p)
            return
        self.last_progress = p
        if self.stack.currentIndex() == P_HOME:      # solo si el panel está a la vista
            self._refresh_dashboard()

    # ============================================================ liberar espacio
    def _find_cleanup(self):
        if not (self.icloud and self.manifest):
            return
        rules = self.settings.cleanup
        cands = find_candidates(self.icloud_items, self.manifest, rules)
        self.cleanup_candidates = cands
        total = sum(c.size for c in cands)
        n_del, b_del = self.manifest.deletion_totals()
        self.cleanup_summary.setText(
            f"{theme.num(len(cands))} elementos verificados en la compu · {theme.size(total)} se pueden liberar. "
            f"Se borran de a {rules.batch_size} por vez (los más pesados primero)."
            + (f"\nYa liberado antes: {theme.num(n_del)} elementos, {theme.size(b_del)}." if n_del else ""))
        t = self.cleanup_table
        t.setSortingEnabled(False)
        t.setRowCount(len(cands))
        for i, c in enumerate(cands):
            it = c.item
            local = c.local_files[0]["local_path"] if c.local_files else "—"
            vals = ["Video" if it.kind == "video" else "Foto",
                    it.capture_date.strftime("%d/%m/%Y") if it.capture_date else "—", theme.size(c.size), local]
            t.setItem(i, 0, QTableWidgetItem(it.name))
            for col, v in enumerate(vals, start=1):
                t.setItem(i, col, QTableWidgetItem(v))
        t.setSortingEnabled(True)
        self._refresh_buttons()

    def _delete_cleanup(self):
        if not (self.icloud and self.manifest and self.cleanup_candidates):
            return
        batch = self.cleanup_candidates[: self.settings.cleanup.batch_size]
        total = sum(c.size for c in batch)
        if not dlg.ask_typed(
                self, f"¿Liberar {len(batch)} elementos ({theme.size(total)}) de iCloud?",
                "Van a «Eliminado recientemente» de iCloud: desaparecen de iCloud y de todos tus dispositivos, y "
                "Apple los guarda 30 días. Antes de cada uno se vuelve a verificar la copia de tu compu.",
                word="BORRAR", yes="Liberar espacio"):
            return
        backend, manifest, cancel = self.icloud, self.manifest, threading.Event()
        self.cancel_event = cancel

        def job(t: TaskThread):
            return delete_from_icloud(backend, manifest, batch, len(batch),
                                      lambda i, n, name: t.status.emit(f"Verificando y liberando {i}/{n}: {name}"),
                                      cancel)

        def ok_(rep):
            msg = f"Liberados {rep.deleted} elementos ({theme.size(rep.freed_bytes)}) de iCloud."
            if rep.skipped:
                msg += "\n\nNo se tocaron (por seguridad):\n" + "\n".join(f"• {n}: {why}" for n, why in rep.skipped)
            self._log(msg)
            self.notify("success", "Espacio liberado en iCloud", msg)
            self._list_icloud(then=self._find_cleanup)

        self._run(job, ok_, None, "Liberando espacio…")

    # ============================================================== automático
    def _auto_tick(self):
        if self.syncing or (self.task and self.task.isRunning() and not self.task.completed):
            return
        if self.settings.auto_sync_usb and self.usb is None and self.manifest:
            if self.chk_demo.isChecked():
                return   # en demo no se conecta solo
            found = self._auto_found
            if found is not None:
                self._auto_found = None
                if any(f.ready for f in found):
                    self._on_detected(found, auto=True)
                    return
            elif self._auto_thread is None or not self._auto_thread.is_alive():
                # la detección (iPhone, Android por MTP/ADB) corre aparte para no trabar la ventana
                probe = self._get_probe()

                def bg():
                    try:
                        self._auto_found = detect_devices(probe)
                    except Exception:  # noqa: BLE001
                        self._auto_found = []

                self._auto_thread = threading.Thread(target=bg, name="Cloudy-detect", daemon=True)
                self._auto_thread.start()
        hours = self.settings.icloud_auto_hours
        if hours and self.icloud and self.manifest and self.icloud_list_time:
            if (datetime.now() - self.icloud_list_time).total_seconds() > hours * 3600:
                self._log("Revisión automática de iCloud…")
                self._list_icloud(then=lambda: self._start_sync(auto=True, sources=["icloud"]))
        self._refresh_chips()
        if self.stack.currentIndex() == P_HOME and not self.syncing:
            self._refresh_dashboard()   # actualiza «hace X min» y el aviso de datos móviles

    # ============================================================== tareas
    def _run(self, fn, on_ok, on_fail, busy_text: str, quiet: bool = False):
        if self.task and self.task.isRunning():
            if not self.task.completed:
                return
            self.task.wait()
        self.task = TaskThread(fn, self)
        self.task.status.connect(self._on_status)
        self.task.progress.connect(self._on_progress)
        self.task.log_line.connect(self._log)

        def done(res):
            on_ok(res)
            self._refresh_buttons(busy=False)

        def failed(msg):
            self._log("ERROR: " + msg)
            if on_fail:
                on_fail(msg)
            if not quiet:
                self._show_error(msg)
            else:
                self.notify("error", "Algo falló", msg, toast=False)
            self._refresh_buttons(busy=False)

        self.task.done.connect(done)
        self.task.failed.connect(failed)
        self._on_status(busy_text)
        self.task.start()
        self._refresh_buttons(busy=True)

    def _on_status(self, msg: str):
        if self.syncing and msg.startswith(("Paso", "desde")):
            self.phase = msg
        if not self.syncing:
            self.line1.setText(msg)
        self._log(msg) if msg and not self.syncing else None

    def _log(self, msg: str):
        if not msg:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {msg}")
        up = msg.upper()
        if up.startswith(("ERROR", "FALL")):
            log.error(msg)
        elif up.startswith(("AVISO", "INTERRUMPIDO", "⚠")):
            log.warning(msg)
        else:
            log.info(msg)

    # ============================================================== avisos
    def notify(self, level: str, title: str, text: str = "", toast: bool = True, alert: bool = True,
               tray: bool = False, action: Optional[tuple[str, Callable]] = None):
        """Un solo lugar para avisar: alerta flotante + centro de alertas + aviso de Windows si no estás mirando."""
        if alert:
            self.alerts.add(level, title, text)
        if toast:
            self.toasts.show(level, title, text, action)
        if tray and self.tray and self.settings.notify_windows and not self.isActiveWindow():
            kind = {"error": QSystemTrayIcon.Critical, "warn": QSystemTrayIcon.Warning}.get(
                level, QSystemTrayIcon.Information)
            self.tray.showMessage(title, text[:240], kind, 8000)

    def _show_error(self, msg: str):
        friendly = [
            ("No hay espacio", "Sin espacio en el disco de destino"),
            ("se llenó", "El disco de destino se llenó"),
            ("Confiar", "El iPhone no confía en esta compu"),
            ("depuración USB", "Falta un permiso en el celular"),
            ("Transferencia de archivos", "No pude abrir el celular"),
            ("Falta ADB", "Faltan las herramientas de Android"),
            ("No se encontró", "No encuentro el celular"),
            ("No encuentro", "No encuentro el celular"),
            ("contraseña", "No se pudo iniciar sesión en iCloud"),
            ("Apple", "Problema con Apple / iCloud"),
            ("PhoneDataBlocked", "Bloqueado para no gastar datos móviles"),
            ("datos móviles", "Bloqueado para no gastar datos móviles"),
        ]
        title = next((t for k, t in friendly if k.lower() in msg.lower()), "Algo salió mal")
        details = None
        head, sep, rest = msg.partition(": ")
        if sep and head.isidentifier() and head[:1].isupper():
            details, msg = msg, rest
        self.alerts.add("error", title, msg)
        dlg.error(self, title, msg, details=details)

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        ic = app_icon()
        self.tray = QSystemTrayIcon(ic, self)
        self.tray.setToolTip(APP_NAME)
        m = QMenu(self)
        m.addAction(f"Abrir {APP_NAME}", lambda: (self.showNormal(), self.raise_(), self.activateWindow()))
        m.addAction("Ver alertas", lambda: (self.showNormal(), self._go(P_ALERTS)))
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and (self.showNormal(), self.raise_(),
                                                                                self.activateWindow()))
        self.tray.messageClicked.connect(lambda: (self.showNormal(), self.raise_(), self.activateWindow()))
        self.tray.show()

    def _refresh_alert_badge(self):
        u = self.alerts.unread()
        self.nav_buttons[P_ALERTS].setText(f"  Alertas ({u})" if u else "  Alertas")
        if hasattr(self, "tile_alerts"):
            last = self.alerts.items[-1] if self.alerts.items else None
            tone = "neutral"
            if u and any(a.level == "error" and not a.read for a in self.alerts.items):
                tone = "bad"
            elif u and any(a.level == "warn" and not a.read for a in self.alerts.items):
                tone = "warn"
            self.tile_alerts.set(f"{u} sin leer" if u else "Al día", (last.title if last else "Sin alertas"), tone)

    def _startup_checks(self):
        if not self.settings.onboarding_done:
            self._show_onboarding()
        self._check_disk(startup=True)
        if self.manifest:
            lv = last_verification(self.manifest)
            days = self.settings.verify_reminder_days
            n_files = len(self.manifest.files_map())
            if days and n_files and (lv["at"] is None or (datetime.now() - lv["at"]).days >= days):
                when = "nunca se verificó" if lv["at"] is None else f"la última verificación fue {theme.ago(lv['at'])}"
                self.notify("info", "Conviene verificar tu copia",
                            f"Tenés {theme.num(n_files)} archivos y {when}. Comprueba que cada uno siga intacto.",
                            action=("Verificar ahora", lambda: (self._go(P_HEALTH), self._verify_all())))
            if lv["problems"]:
                self.notify("warn", "La última verificación encontró problemas",
                            f"{lv['problems']} archivo(s) faltantes o dañados.",
                            action=("Ver en Salud", lambda: self._go(P_HEALTH)), alert=False)

    def _check_disk(self, startup: bool = False):
        if not self.settings.destination:
            return
        di = disk_info(self.settings.destination)
        if not di:
            if startup:
                self.notify("error", "No encuentro la carpeta destino",
                            f"{self.settings.destination}\n¿Está desconectado el disco externo?")
            return
        if di.low_space and not self._low_space_warned:
            self._low_space_warned = True
            self.notify("warn", "Queda poco espacio en el disco",
                        f"Libres: {theme.size(di.free)} de {theme.size(di.total)} ({int(di.used_ratio * 100)} % usado).",
                        tray=not startup)

    def _show_onboarding(self):
        d = OnboardingDialog(self, self._pick_destination_for_onboarding, self.settings.destination,
                             self._options().limit)
        d.run()
        self.settings.onboarding_done = True
        self._save()
        self._refresh_all()

    def _after_sync(self, reports, failed: int, summary: str, auto: bool):
        items = sum(r.items_done for _n, r in reports)
        size = sum(r.copied_bytes for _n, r in reports)
        cancelled = any(r.cancelled for _n, r in reports)
        interrupted = next((r.interrupted for _n, r in reports if r.interrupted), None)
        if interrupted:
            self.notify("warn", "La copia se interrumpió", interrupted + " Lo verificado quedó guardado.", tray=True,
                        action=("Reintentar", self._start_sync))
        elif cancelled:
            self.notify("info", "Copia cancelada", "Lo verificado quedó guardado; lo parcial se retoma la próxima vez.")
        elif failed:
            self.notify("error", f"Copia terminada con {failed} error(es)",
                        f"{theme.num(items)} elementos copiados ({theme.size(size)}). Tocá «Copiar ahora» para "
                        "reintentar los que fallaron.", tray=True, action=("Ver registro", lambda: self._go(P_LOG)))
        else:
            self.notify("success", "Copia terminada y verificada",
                        f"{theme.num(items)} elementos · {theme.size(size)} · todo comprobado con SHA-256.", tray=True,
                        action=("Abrir carpeta", self._open_destination))
        if not auto and (failed or interrupted):
            dlg.warn(self, "La copia no terminó del todo", summary.split("\n")[0], details=summary)
        self._low_space_warned = False
        self._check_disk()

    # ============================================================== Salud
    def _build_health(self) -> QWidget:
        w, lay = page("Salud de tu copia", "Comprueba que cada archivo guardado siga idéntico al que se copió, "
                                           "y que tu carpeta esté protegida.")
        head, hv = card()
        top = QHBoxLayout()
        top.setSpacing(16)
        self.health_badge = QLabel()
        self.health_badge.setFixedSize(64, 64)
        self.health_badge.setAlignment(Qt.AlignCenter)
        top.addWidget(self.health_badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.health_title = label("", "big", wrap=True)
        self.health_sub = label("", "muted", wrap=True)
        col.addWidget(self.health_title)
        col.addWidget(self.health_sub)
        top.addLayout(col, 1)
        hv.addLayout(top)
        self.health_bar = QProgressBar()
        self.health_bar.setTextVisible(False)
        self.health_bar.setRange(0, 1000)
        self.health_bar.hide()
        hv.addWidget(self.health_bar)
        self.health_line = label("", "small", wrap=True)
        hv.addWidget(self.health_line)
        hb = QHBoxLayout()
        self.btn_verify = button("Verificar todo", "primary", self._verify_all)
        self.btn_verify.setMinimumHeight(36)
        self.btn_verify_cancel = button("Detener", None, lambda: self.cancel_event.set())
        self.btn_repair = button("Reparar", None, self._repair)
        self.btn_backup = button("Respaldar catálogo", None, self._backup_catalog)
        for b in (self.btn_verify, self.btn_verify_cancel, self.btn_repair, self.btn_backup):
            hb.addWidget(b)
        hb.addStretch(1)
        hv.addLayout(hb)
        lay.addWidget(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        self.ht_files = Tile("photos", "Archivos en el catálogo")
        self.ht_disk = Tile("disk", "Disco de destino")
        self.ht_backup = Tile("folder", "Respaldos del catálogo")
        self.ht_parts = Tile("wrench", "Copias a medias")
        for t in (self.ht_files, self.ht_disk, self.ht_backup, self.ht_parts):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        prob, pv = card("Problemas encontrados", "warn", theme.ORANGE)
        self.problems_table = QTableWidget(0, 3)
        self.problems_table.setHorizontalHeaderLabels(["Archivo", "Problema", "Detalle"])
        self._style_table(self.problems_table)
        self.problems_table.setMinimumHeight(160)
        pv.addWidget(self.problems_table)
        pv.addWidget(label("«Reparar» saca esos archivos del catálogo (los dañados se renombran a «.danado», no se "
                           "borran) y la próxima «Copiar ahora» los vuelve a traer del iPhone o de iCloud.",
                           "small", wrap=True))
        self.problems_card = prob
        prob.hide()
        lay.addWidget(prob)

        sec, sv = card("Seguridad y privacidad", "lock", theme.GREEN)
        self.security_rows = QVBoxLayout()
        self.security_rows.setSpacing(6)
        sv.addLayout(self.security_rows)
        lay.addWidget(sec)
        lay.addStretch(1)
        return w

    def _security_checks(self) -> list[tuple[bool, str, str]]:
        dest = self.settings.destination
        hs = hotspot_interfaces()
        checks = [
            (no_network.is_installed(), "Bloqueo de internet activo",
             "Solo se permite la conexión local con el celular por cable" +
             (" y los dominios de Apple (iCloud conectado)." if no_network.icloud_allowed() else ".")),
            (not hs or not self.settings.icloud_allow_phone_data, "Sin datos móviles",
             "La compu sale por el celular: iCloud bloqueado." if hs and not self.settings.icloud_allow_phone_data else
             ("Atención: permitiste iCloud por datos móviles." if hs else "La compu no está usando el hotspot del celular.")),
            (not (dest and is_cloud_folder(dest)), "Fotos solo en tu compu",
             "La carpeta destino no se sincroniza con ninguna nube." if dest and not is_cloud_folder(dest) else
             ("La carpeta destino parece de OneDrive/Drive/Dropbox: se sube a esa nube." if dest else
              "Todavía no elegiste carpeta.")),
            (True, "Contraseña de Apple nunca guardada", "Solo se guarda la sesión; «Cerrar sesión» la borra."),
            (True, "Registros sin datos personales", "Correos, códigos y tokens se enmascaran antes de escribirse."),
            (True, "Nada se borra del celular", "Android, tarjetas y cámaras se abren en solo lectura. Solo «Liberar "
             "espacio» toca iCloud, y únicamente lo ya verificado."),
        ]
        cloud_mirrors = [m for m in self.settings.options.mirror_destinations if is_cloud_folder(m)]
        if cloud_mirrors:
            checks.append((False, "Copia espejo en la nube", "Una copia espejo se sube a: " + ", ".join(cloud_mirrors)))
        return checks

    def _refresh_health(self):
        if not hasattr(self, "health_title"):
            return
        # seguridad
        while self.security_rows.count():
            it = self.security_rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for ok, title, detail in self._security_checks():
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 2, 0, 2)
            h.setSpacing(10)
            ic = QLabel()
            ic.setPixmap(icons.pixmap("check" if ok else "warn", theme.GREEN if ok else theme.ORANGE, 18, 2.0))
            h.addWidget(ic, 0, Qt.AlignTop)
            t = label(f"<b>{title}</b> — <span style='color:{theme.MUTED}'>{detail}</span>", None, wrap=True)
            h.addWidget(t, 1)
            self.security_rows.addWidget(row)

        busy = bool(self.task and self.task.isRunning() and not self.task.completed)
        verifying = getattr(self, "_verifying", False)
        self.btn_verify.setEnabled(bool(self.manifest) and not busy and not self.syncing)
        self.btn_verify_cancel.setVisible(verifying)
        self.btn_backup.setEnabled(bool(self.manifest) and not busy)
        rep_ = self.verify_report
        self.btn_repair.setEnabled(bool(rep_ and rep_.problems) and not busy)
        if not self.manifest:
            self._set_health("info", "Elegí una carpeta destino", "Cuando haya fotos copiadas, acá vas a poder "
                                                                   "verificarlas.")
            for t in (self.ht_files, self.ht_disk, self.ht_backup, self.ht_parts):
                t.set("—")
            return
        files = self.manifest.all_files()
        total_b = sum(f["size"] for f in files)
        self.ht_files.set(theme.num(len(files)), theme.size(total_b))
        di = disk_info(self.manifest.root)
        if di:
            tone = "warn" if (di.low_space or di.is_fat) else "good"
            extra = " · FAT32 (máx. 4 GB por archivo)" if di.is_fat else ""
            self.ht_disk.set(f"{theme.size(di.free)} libres", f"de {theme.size(di.total)} · "
                             f"{di.fstype.upper() if di.fstype != '?' else 'formato ?'}{extra}", tone)
        bks = list_backups(self.manifest.dir)
        self.ht_backup.set(str(len(bks)), ("Último: " + theme.ago(datetime.fromtimestamp(bks[0].stat().st_mtime)))
                           if bks else "Todavía ninguno (se crea solo al copiar)", "good" if bks else "warn")
        parts = getattr(self, "_parts_cache", None)
        if parts is None:
            parts = self._parts_cache = len(list(self.manifest.root.rglob("*.part")))
        self.ht_parts.set(str(parts), "Se retoman solas en la próxima copia" if parts else "Ninguna",
                          "warn" if parts else "good")

        if verifying:
            return
        lv = last_verification(self.manifest)
        if not files:
            self._set_health("info", "Todavía no hay nada copiado", "Hacé una copia y después verificala acá.")
        elif rep_ and rep_.cancelled:
            self._set_health("warn", "Verificación detenida", f"Se revisaron {theme.num(rep_.checked)} de "
                                                               f"{theme.num(len(files))} archivos.")
        elif lv["at"] is None:
            self._set_health("warn", "Nunca se verificó", "Tocá «Verificar todo»: lee cada archivo del disco y lo "
                                                          "compara con su huella SHA-256 original.")
        elif lv["problems"]:
            self._set_health("bad", f"{lv['problems']} archivo(s) con problemas",
                             f"Verificado {theme.ago(lv['at'])}. Tocá «Reparar» para volver a traerlos.")
        else:
            self._set_health("good", "Tu copia está sana",
                             f"{theme.num(lv['checked'])} archivos idénticos a los originales · verificado "
                             f"{theme.ago(lv['at'])}.")
        if rep_:
            t = self.problems_table
            t.setRowCount(len(rep_.problems))
            for i, pr in enumerate(rep_.problems):
                for c, v in enumerate((pr.local_path, pr.kind.capitalize(), pr.detail)):
                    t.setItem(i, c, QTableWidgetItem(v))
            self.problems_card.setVisible(bool(rep_.problems))

    def _set_health(self, tone: str, title: str, sub: str):
        color = {"good": theme.GREEN, "warn": theme.ORANGE, "bad": theme.RED}.get(tone, theme.ACCENT_HOVER)
        bg = {"good": "#123227", "warn": "#33280f", "bad": "#3a1620"}.get(tone, "#16244a")
        ic = {"good": "shield", "warn": "warn", "bad": "error"}.get(tone, "info")
        self.health_badge.setStyleSheet(f"background:{bg}; border-radius:32px;")
        self.health_badge.setPixmap(icons.pixmap(ic, color, 32, 2.0))
        self.health_title.setText(title)
        self.health_sub.setText(sub)

    def _verify_all(self):
        if not self.manifest or self.syncing:
            return
        if self.task and self.task.isRunning() and not self.task.completed:
            return
        manifest = self.manifest
        cancel = threading.Event()
        self.cancel_event = cancel
        self._verifying = True
        self.health_bar.setValue(0)
        self.health_bar.show()
        self._set_health("info", "Verificando…", "Leyendo cada archivo del disco y comparando su SHA-256. La compu no "
                                                 "se suspende mientras tanto.")
        struct = self.chk_struct.isChecked()

        def job(t: TaskThread):
            def prog(i, n, done_b, total_b, name):
                t.progress.emit(("verify", i, n, done_b, total_b, name))
            with KeepAwake():
                return verify_library(manifest, prog, cancel, structure=struct)

        def ok(rep_):
            self._verifying = False
            self.health_bar.hide()
            self.health_line.setText("")
            self.verify_report = rep_
            self._parts_cache = None
            mins = int(rep_.seconds // 60)
            took = f"{mins} min {int(rep_.seconds % 60)} s" if mins else f"{int(rep_.seconds)} s"
            self._log(f"Verificación: {rep_.checked} revisados, {rep_.ok} OK, {len(rep_.problems)} problemas, "
                      f"{len(rep_.leftovers)} .part, en {took}")
            for pr in rep_.problems:
                self._log(f"ERROR verificación {pr.local_path}: {pr.kind} — {pr.detail}")
            for wmsg in rep_.structure_warnings:
                self._log(f"AVISO estructura: {wmsg}")
            if rep_.cancelled:
                self.notify("info", "Verificación detenida", f"Se revisaron {theme.num(rep_.checked)} archivos.",
                            alert=False)
            elif rep_.problems:
                self.notify("error", f"{len(rep_.problems)} archivo(s) con problemas",
                            "Faltan o cambiaron desde que se copiaron. «Reparar» los vuelve a traer.", tray=True,
                            action=("Ver en Salud", lambda: self._go(P_HEALTH)))
            else:
                self.notify("success", "Tu copia está sana",
                            f"{theme.num(rep_.ok)} archivos ({theme.size(rep_.bytes_checked)}) idénticos a los "
                            f"originales. Tardó {took}.", tray=True)
            self._refresh_health()

        def fail(_m):
            self._verifying = False
            self.health_bar.hide()
            self._refresh_health()

        self._run(job, ok, fail, "Verificando la copia…")
        self._refresh_health()

    def _on_verify_progress(self, data):
        _tag, i, n, done_b, total_b, name = data
        frac = done_b / total_b if total_b else (i / n if n else 0)
        self.health_bar.setValue(int(frac * 1000))
        self.health_line.setText(f"{theme.num(i)} de {theme.num(n)} · {theme.gb_pair(done_b, total_b)} · {name}")

    def _repair(self):
        rep_ = self.verify_report
        if not (self.manifest and rep_ and rep_.problems):
            return
        if not dlg.ask(self, f"¿Reparar {len(rep_.problems)} archivo(s)?",
                       "Se sacan del catálogo para que la próxima «Copiar ahora» los vuelva a traer del iPhone o "
                       "de iCloud. Los dañados se renombran a «.danado» (no se borran) por si querés revisarlos.",
                       yes="Reparar"):
            return
        n = repair(self.manifest, rep_.problems)
        self.manifest.set_meta("verify_last_problems", "0")
        self.verify_report = None
        self._log(f"Reparación: {n} archivo(s) marcados para volver a copiar.")
        self.notify("success", "Listo para volver a copiar",
                    f"{n} archivo(s) se van a traer de nuevo en la próxima copia (conectá el iPhone o iCloud).")
        self.problems_card.hide()
        self._refresh_all()
        self._refresh_health()

    def _backup_catalog(self):
        if not self.manifest:
            return
        try:
            p = self.manifest.backup()
            self.notify("success", "Catálogo respaldado", p.name, alert=False)
        except Exception as e:  # noqa: BLE001
            dlg.error(self, "No pude respaldar el catálogo", str(e))
        self._refresh_health()

    # ============================================================== Alertas
    def _build_alerts(self) -> QWidget:
        w, lay = page("Alertas", "Lo importante que pasó: copias terminadas, errores, avisos del disco y "
                                 "verificaciones.")
        self.alerts_panel = AlertsPanel(self.alerts)
        lay.addWidget(self.alerts_panel, 1)
        return w

    def closeEvent(self, event):
        if self.syncing:
            if not dlg.ask(self, "Hay una copia en curso",
                           "¿Cancelarla y salir? Lo verificado queda guardado y lo parcial se retoma la próxima vez.",
                           yes="Cancelar y salir", no="Seguir copiando", danger=True):
                event.ignore()
                return
            self.pause_event.clear()
            self.cancel_event.set()
            if self.task:
                self.task.wait(15_000)
        self._save()
        for b in (self.usb, self._probe):
            try:
                if b:
                    getattr(b, "shutdown", b.close)()
            except Exception:  # noqa: BLE001
                pass
        for t in (self._auto_timer, self._refresh_timer):
            t.stop()
        if self.tray:
            self.tray.hide()
        if self.manifest:
            self.manifest.close()
            self.manifest = None
        event.accept()


def run_gui() -> int:
    from ...security.logs import get_logger
    get_logger().info("Inicio de %s %s (interfaz gráfica)", APP_NAME, __version__)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    if sys.platform == "win32":   # que la barra de tareas muestre el logo de Cloudy y no el de Python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cloudy.App")
        except Exception:  # noqa: BLE001
            pass
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)
    w = MainWindow()
    w.show()
    return app.exec()
