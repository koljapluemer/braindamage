"""Skin picker dialog: search-first selection of one trade-up input from the
full eligible-options list (thousands of entries) -- port of the Textual
SkinPickerModal, now backed by the shared SearchFilterBar for debounced,
off-thread filtering instead of a per-keystroke synchronous scan.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from ...tradeup import SkinOption
from ..widgets.search_filter_bar import SearchFilterBar

_MAX_RESULTS = 200


def _filter_options(options: list[SkinOption], query: str) -> list[SkinOption]:
    needle = query.strip().lower()
    matches = options if not needle else [o for o in options if needle in o.label.lower()]
    return matches[:_MAX_RESULTS]


class SkinPickerDialog(QDialog):
    def __init__(self, options: list[SkinOption], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add trade-up input")
        self.resize(640, 480)
        self._by_id = {option.skin_id: option for option in options}
        self._picked: SkinOption | None = None

        self._search_bar = SearchFilterBar(_filter_options, placeholder="Search skins...", parent=self)
        self._search_bar.line_edit.returnPressed.connect(self._accept_current)
        self._search_bar.resultsReady.connect(self._populate)

        self._list = QListWidget(self)
        self._list.itemActivated.connect(self._accept_item)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Type to search, Enter/double-click to pick, Esc to cancel."))
        layout.addWidget(self._search_bar)
        layout.addWidget(self._list)
        layout.addWidget(buttons)

        self._search_bar.set_dataset(options)
        self._search_bar.focus()

    def _populate(self, options: list[SkinOption]) -> None:
        self._list.clear()
        for option in options:
            item = QListWidgetItem(option.label)
            item.setData(Qt.ItemDataRole.UserRole, option.skin_id)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _accept_item(self, item: QListWidgetItem) -> None:
        skin_id = item.data(Qt.ItemDataRole.UserRole)
        self._picked = self._by_id.get(skin_id)
        self.accept()

    def _accept_current(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self._accept_item(item)

    def picked_option(self) -> SkinOption | None:
        return self._picked
