"""QAbstractTableModel for a contract's input lines -- shared by the builder
page's lines table and the contract detail dialog's historical view. Rows are
plain dicts with keys skin_name/collection_name/float_value/quantity so both
callers (live tradeup.ContractLine objects and persisted Contract.input_lines
JSON, which already uses this same key shape) can feed it without a
ContractLine-specific dependency in the model itself.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...tradeup import ContractLine, wear_for_float

_HEADERS = ["Skin", "Collection", "Wear", "Float", "Qty"]
_HEADER_TOOLTIPS = [
    "Input skin used in this line.",
    "Skin collection this input belongs to — determines which output skins it can produce.",
    "Wear condition derived from this input's float value.",
    "Exact float value of this input skin.",
    "How many copies of this input are used (a trade-up contract always consumes 10 inputs total).",
]


def line_to_row(line: ContractLine) -> dict:
    return {
        "skin_id": line.skin_id,
        "skin_name": line.skin_name,
        "collection_id": line.collection_id,
        "collection_name": line.collection_name,
        "float_value": line.float_value,
        "quantity": line.quantity,
    }


class LineTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> dict | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return _HEADER_TOOLTIPS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()]
        float_value = row["float_value"]
        match index.column():
            case 0:
                return row["skin_name"]
            case 1:
                return row["collection_name"]
            case 2:
                return wear_for_float(float_value)
            case 3:
                return f"{float_value:.4f}"
            case 4:
                return str(row["quantity"])
        return None
