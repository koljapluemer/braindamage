"""Skins page: read-only browse of the full skin catalog with live filter.
Port of the Textual SkinsScreen -- shares SkinTableModel/SearchFilterBar with
the Maintenance page, only the column set and absence of action buttons
differ.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget

from ...models import Skin
from ..models.skin_table_model import SKINS_COLUMNS, SkinTableModel
from ..skin_dataset import filter_skins, load_skins
from ..widgets.search_filter_bar import SearchFilterBar


class SkinsPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._search_bar = SearchFilterBar(
            filter_skins, placeholder="Filter by name or collection...", parent=self
        )
        self._search_bar.resultsReady.connect(self._on_results)
        self._search_bar.loadFailed.connect(self._on_load_failed)

        self._model = SkinTableModel(SKINS_COLUMNS, self)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 320)

        self._status_label = QLabel("", self)

        layout = QVBoxLayout(self)
        layout.addWidget(self._search_bar)
        layout.addWidget(self._table)
        layout.addWidget(self._status_label)

    def on_page_shown(self) -> None:
        self._search_bar.load(load_skins)

    def _on_results(self, rows: list[Skin]) -> None:
        self._model.set_rows(rows)
        self._status_label.setText(f"{len(rows)} skins")

    def _on_load_failed(self, message: str) -> None:
        self._status_label.setText(f"Error loading skins: {message}")
