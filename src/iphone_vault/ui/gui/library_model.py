"""Modelo de la tabla «Fototeca»: une lo que hay en el iPhone y en iCloud (la misma foto aparece una vez)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from ...models import AVAIL_CLOUD_ONLY, AVAIL_OPTIMIZED, MediaItem
from . import theme

ST_DONE = "Ya copiado"
ST_WILL = "Se va a copiar"
ST_WILL_CLOUD = "Se baja de iCloud"
ST_PENDING = "Pendiente: solo en iCloud"
ST_OPTIMIZED = "Pendiente: versión optimizada"
ST_EXCLUDED = "Excluido por opciones"
ST_USER_EXCLUDED = "Excluido por vos"
ST_LIMIT = "Fuera del límite de prueba"
ST_TRASH = "En «Eliminado recientemente»"
ST_FAILED = "Error (se reintenta)"
ST_DELETED = "Liberado de iCloud"

COLORS = {
    ST_DONE: theme.GREEN, ST_WILL: theme.ACCENT_HOVER, ST_WILL_CLOUD: theme.ACCENT_HOVER,
    ST_PENDING: theme.RED, ST_OPTIMIZED: theme.RED, ST_FAILED: theme.ORANGE,
}

FILTERS = {
    "Todos": None,
    "Se va a copiar": {ST_WILL, ST_WILL_CLOUD},
    "Ya copiados": {ST_DONE, ST_DELETED},
    "Pendientes": {ST_PENDING, ST_OPTIMIZED},
    "Excluidos": {ST_EXCLUDED, ST_USER_EXCLUDED, ST_LIMIT, ST_TRASH},
    "Con error": {ST_FAILED},
}

HEADERS = ["Archivo", "Tipo", "Fecha de captura", "Tamaño", "Origen", "Estado"]


@dataclass
class Row:
    item: MediaItem                    # el que se muestra (iPhone si está, si no iCloud)
    alt: Optional[MediaItem] = None    # la misma foto en la otra fuente
    status: str = ""
    local_path: Optional[str] = None

    @property
    def origin(self) -> str:
        srcs = {self.item.source} | ({self.alt.source} if self.alt else set())
        if srcs == {"usb", "icloud"}:
            return "iPhone + iCloud"
        return "iCloud" if "icloud" in srcs else "iPhone"

    @property
    def kind_label(self) -> str:
        it = self.item
        k = "Video" if it.kind == "video" else ("Live Photo" if it.is_live else "Foto")
        if it.is_screenshot:
            k = "Captura"
        if it.has_edits:
            k += " · editada"
        return k


def merge_rows(usb: list[MediaItem], icloud: list[MediaItem]) -> list[Row]:
    rows: list[Row] = []
    by_fp: dict[str, Row] = {}
    for it in usb:
        r = Row(it)
        rows.append(r)
        if it.fingerprint:
            by_fp[it.fingerprint] = r
    for it in icloud:
        r = by_fp.get(it.fingerprint or "")
        if r is not None and r.alt is None:
            r.alt = it
        else:
            rows.append(Row(it))
    return rows


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Row] = []
        self.cells: list[tuple] = []
        self.keys: list[tuple] = []
        self.sort_col, self.sort_order = 2, Qt.DescendingOrder

    def _sorted(self, rows, cells, keys):
        idx = sorted(range(len(rows)), key=lambda i: keys[i][self.sort_col],
                     reverse=self.sort_order == Qt.DescendingOrder)
        return [rows[i] for i in idx], [cells[i] for i in idx], [keys[i] for i in idx]

    def sort(self, column, order=Qt.AscendingOrder):
        """Orden en Python (una sola pasada con sorted): mucho más rápido que comparar fila por fila en Qt."""
        self.sort_col, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        self.rows, self.cells, self.keys = self._sorted(self.rows, self.cells, self.keys)
        self.layoutChanged.emit()

    @staticmethod
    def _size(r: Row) -> int:
        it = r.item
        if it.availability in (AVAIL_CLOUD_ONLY, AVAIL_OPTIMIZED) and r.alt:
            return r.alt.best_size
        return it.best_size

    def set_rows(self, rows: list[Row]):
        """Precalcula textos y claves de orden: con 30.000 filas, ordenar llama a data() cientos de miles
        de veces, así que todo lo que se pueda se calcula una sola vez acá."""
        cells, keys = [], []
        for r in rows:
            it = r.item
            size = self._size(r)
            date_txt = it.capture_date.strftime("%d/%m/%Y %H:%M") if it.capture_date else "—"
            cells.append((it.name, r.kind_label, date_txt, theme.size(size), r.origin, r.status))
            keys.append((it.name.lower(), r.kind_label, it.capture_date.timestamp() if it.capture_date else 0.0,
                         float(size), r.origin, r.status))
        rows, cells, keys = self._sorted(rows, cells, keys)
        self.beginResetModel()
        self.rows, self.cells, self.keys = rows, cells, keys
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, c = index.row(), index.column()
        if role == Qt.DisplayRole:
            return self.cells[row][c]
        r = self.rows[row]
        it = r.item
        if role == Qt.UserRole:   # clave de orden
            return self.keys[row][c]
        if role == Qt.ForegroundRole and c == 5:
            col = COLORS.get(r.status)
            return QColor(col) if col else QColor(theme.MUTED)
        if role == Qt.ToolTipRole and c == 0:
            return it.primary_path if it.source == "usb" else f"iCloud · {it.name}"
        return None


class LibraryProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.statuses: Optional[set[str]] = None
        self.source_filter = "Todos"
        self.setDynamicSortFilter(False)

    def set_filters(self, text: str, statuses, source: str):
        self.text = text.lower().strip()
        self.statuses = statuses
        self.source_filter = source
        self.invalidateFilter()

    def sort(self, column, order=Qt.AscendingOrder):
        self.sourceModel().sort(column, order)   # el modelo ya ordena; el proxy solo filtra

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        r: Row = self.sourceModel().rows[source_row]
        if self.statuses is not None and r.status not in self.statuses:
            return False
        if self.source_filter == "iPhone" and "iPhone" not in r.origin:
            return False
        if self.source_filter == "iCloud" and "iCloud" not in r.origin:
            return False
        if self.text and self.text not in r.item.name.lower() and self.text not in " ".join(r.item.albums).lower():
            return False
        return True
