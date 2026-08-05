"""QAbstractTableModel for Skin rows, parameterized by a column list so the
Maintenance and Skins pages -- whose column sets differ only slightly -- can
share one model class instead of duplicating population code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...models import Skin


def _fmt_price(price: float | None) -> str:
    return f"${price:.2f}" if price is not None else "—"


def _fmt_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "—"


def _fmt_variant(skin: Skin) -> str:
    if skin.stattrak:
        return "StatTrak"
    if skin.souvenir:
        return "Souvenir"
    return "Normal"


@dataclass(frozen=True)
class ColumnSpec:
    header: str
    value: Callable[[Skin], str]


NAME = ColumnSpec("Name", lambda s: s.name)
COLLECTION = ColumnSpec("Collection", lambda s: s.collection_name or "—")
RARITY = ColumnSpec("Rarity", lambda s: s.rarity_name or "—")
VARIANT = ColumnSpec("Variant", _fmt_variant)
LAST_PRICE = ColumnSpec("Last Price", lambda s: _fmt_price(s.last_price))
DATA_RECENCY = ColumnSpec(
    "Data Recency", lambda s: _fmt_datetime(s.last_price_calculation_data_point_recency)
)
RECALCULATED_AT = ColumnSpec("Recalculated At", lambda s: _fmt_datetime(s.last_price_recalculated_at))

MAINTENANCE_COLUMNS = [NAME, COLLECTION, RARITY, VARIANT, LAST_PRICE, RECALCULATED_AT]
SKINS_COLUMNS = [NAME, COLLECTION, RARITY, VARIANT, LAST_PRICE, DATA_RECENCY, RECALCULATED_AT]


class SkinTableModel(QAbstractTableModel):
    def __init__(self, columns: list[ColumnSpec], parent=None) -> None:
        super().__init__(parent)
        self._columns = columns
        self._rows: list[Skin] = []

    def set_rows(self, rows: list[Skin]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def skin_at(self, row: int) -> Skin | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return self._columns[section].header

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._columns[index.column()].value(self._rows[index.row()])
