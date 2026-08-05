"""QAbstractTableModel for a simulation's outcome distribution -- shared by
the builder page's live results and the contract detail dialog's historical
view. Rows are plain dicts (probability/skin_name/predicted_wear/net_price/
contribution), matching the shape Contract.outcomes already stores as JSON, so
the caller for a live (not-yet-persisted) SimulationResult just needs to
convert its Outcome dataclasses to dicts once (dataclasses.asdict).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

_HEADERS = ["Probability", "Skin", "Wear", "Net Price", "Contribution"]


class OutcomeTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.default_sort()
        self.endResetModel()

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
                return f"{row['probability']:.2%}"
            case 1:
                return row["skin_name"]
            case 2:
                return row["predicted_wear"]
            case 3:
                net_price = row.get("net_price")
                return f"${net_price:.2f}" if net_price is not None else "—"
            case 4:
                return f"${row['contribution']:.2f}"
        return None

    _SORT_KEYS = {
        0: lambda r: r["probability"],
        1: lambda r: r["skin_name"],
        2: lambda r: r["predicted_wear"],
        3: lambda r: r["net_price"] if r.get("net_price") is not None else float("-inf"),
        4: lambda r: r["contribution"],
    }

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = self._SORT_KEYS.get(column)
        if key is None:
            return
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.layoutChanged.emit()

    def default_sort(self) -> None:
        self._rows.sort(key=lambda r: r["probability"], reverse=True)
