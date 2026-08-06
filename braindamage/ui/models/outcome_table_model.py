"""QAbstractTableModel for a simulation's outcome distribution -- shared by
the builder page's live results and the contract detail dialog's historical
view. Rows are plain dicts (probability/skin_name/predicted_wear/net_price/
contribution), matching the shape Contract.outcomes already stores as JSON, so
the caller for a live (not-yet-persisted) SimulationResult just needs to
convert its Outcome dataclasses to dicts once (dataclasses.asdict).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...tradeup import SELL_FEE_RATE

_HEADERS = ["Probability", "Skin", "Wear", "Net Price", "Contribution"]
_HEADER_TOOLTIPS = [
    "Chance the trade-up produces this exact skin. Formula: (your inputs from this skin's collection ÷ 10) "
    "× (1 ÷ number of eligible output skins in that collection at the next rarity). E.g. 3 of your 10 inputs "
    "from a collection with 4 possible outputs there → (3/10) × (1/4) = 7.5% for each of those 4 skins.",
    "One specific skin this contract could output — every row is a different possible result, not a guarantee.",
    "Wear this specific output skin would land in, computed from your inputs' average float remapped through "
    "this skin's own [min float, max float] range (not the raw average float itself).",
    f"What you'd actually receive selling THIS row's skin on Steam at current market price: "
    f"gross price × (1 − {SELL_FEE_RATE:.0%} sell fee) = gross price × {1 - SELL_FEE_RATE:.2f}. "
    "Net of Steam's marketplace cut only — nothing else is subtracted here.",
    "This row's slice of the contract's total expected revenue: Probability × Net Price. Sum every row's "
    "Contribution and you get the total expected payout that Expected Value (top) subtracts Input Cost from — "
    "so a 5% chance at $50 (Contribution $2.50) counts the same as a 50% chance at $5.",
]


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
