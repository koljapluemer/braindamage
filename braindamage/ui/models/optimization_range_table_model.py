from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

_HEADERS = ["Min float", "Max float", "Expected price", "Outcome", "ROI", "CVaR"]


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
        return _HEADERS[section] if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal else None

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
