from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

_HEADERS = ["Min float", "Max float", "Expected price", "Outcome", "ROI", "CVaR"]
_HEADER_TOOLTIPS = [
    "Lower bound of this buying range: the lowest average input float that still produces the same "
    "expected outcome and price.",
    "Upper bound of this buying range: the highest average input float that still produces the same "
    "expected outcome and price.",
    "Expected sale revenue (after the Steam sell fee) if your inputs' average float falls in this range.",
    "Risk classification for this range: 'Guaranteed profit' means every possible outcome outsells the "
    "input cost; 'Positive/Negative EV' means the average outcome is profitable/unprofitable, but individual "
    "rolls can still go the other way.",
    "Expected profit as a percentage of input cost, for this float range.",
    "Conditional Value at Risk (5%): average profit across the worst 5% of outcomes, for this float range.",
]


class OptimizationRangeTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return _HEADER_TOOLTIPS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()]
        values = (
            f"{row['min_float']:.5f}", f"{row['max_float']:.5f}",
            f"${row['expected_price']:.2f}", row["outcome"],
            f"{row['roi']:.1%}" if row["roi"] is not None else "—",
            f"${row['cvar_5pct']:+.2f}" if row["cvar_5pct"] is not None else "—",
        )
        return values[index.column()]
