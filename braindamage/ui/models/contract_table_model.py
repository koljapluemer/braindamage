"""QAbstractTableModel for Contract rows (the Contracts page). Implements
sort() directly against the underlying numeric/boolean fields rather than
relying on a QSortFilterProxyModel comparing formatted display strings, so
e.g. EV/ROI/CVaR sort numerically, not lexicographically.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...models import Contract

_HEADERS = ["Fav", "Rarity", "Variant", "Input Cost", "EV", "ROI", "CVaR (5%)", "Last Simulated"]


class ContractTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Contract] = []

    def set_rows(self, rows: list[Contract]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.default_sort()
        self.endResetModel()

    def contract_at(self, row: int) -> Contract | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return _HEADERS[section]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()]
        match index.column():
            case 0:
                return "★" if row.favorite else "☆"
            case 1:
                return row.rarity_name
            case 2:
                return "StatTrak" if row.stattrak else "Normal"
            case 3:
                return f"${row.input_cost:.2f}"
            case 4:
                return f"${row.expected_value:+.2f}"
            case 5:
                return f"{row.roi:.1%}" if row.roi is not None else "—"
            case 6:
                return f"${row.cvar_5pct:.2f}" if row.cvar_5pct is not None else "—"
            case 7:
                return row.last_simulated_at.strftime("%Y-%m-%d %H:%M")
        return None

    _SORT_KEYS = {
        0: lambda r: r.favorite,
        1: lambda r: r.rarity_name,
        2: lambda r: r.stattrak,
        3: lambda r: r.input_cost,
        4: lambda r: r.expected_value,
        5: lambda r: r.roi if r.roi is not None else float("-inf"),
        6: lambda r: r.cvar_5pct if r.cvar_5pct is not None else float("-inf"),
        7: lambda r: r.last_simulated_at,
    }

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = self._SORT_KEYS.get(column)
        if key is None:
            return
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.layoutChanged.emit()

    def default_sort(self) -> None:
        """Favorited first, then highest expected value -- matches the
        previous Textual screen's fixed query ordering, kept as the initial
        sort here even though the user can now click any header to re-sort."""
        self._rows.sort(key=lambda r: (r.favorite, r.expected_value), reverse=True)
