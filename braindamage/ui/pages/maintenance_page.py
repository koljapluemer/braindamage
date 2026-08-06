"""Maintenance page: the app's admin actions -- fetch current prices for a
single skin via the CS2Cap API, and (re)generate mono-trade contracts. Catalog
import and bulk historical import remain throwaway scripts (see scripts/), not
UI actions, since they're infrequent one-off operations. Port of the Textual
MaintenanceScreen, plus the new mono-trades button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...models import Skin
from ...tradeup import INPUT_RARITIES
from ..models.skin_table_model import MAINTENANCE_COLUMNS, SkinTableModel
from ..skin_dataset import filter_skins, load_skins
from ..widgets.search_filter_bar import SearchFilterBar
from ..workers.mono_trade_worker import MonoTradeWorker
from ..workers.price_fetch_worker import PriceFetchWorker
from ..workers.signals import keep_alive


class MaintenancePage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._inflight: list = []

        self._search_bar = SearchFilterBar(
            filter_skins, placeholder="Search skins by name or collection...", parent=self
        )
        self._search_bar.resultsReady.connect(self._on_results)
        self._search_bar.loadFailed.connect(self._on_load_failed)

        self._model = SkinTableModel(MAINTENANCE_COLUMNS, self)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 320)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        self._status_label = QLabel(
            "Select a skin and click 'Fetch prices' to fetch its current prices.", self
        )

        self._fetch_button = QPushButton("Fetch prices for selected", self)
        self._fetch_button.setEnabled(False)
        self._fetch_button.clicked.connect(self._fetch_prices)

        self._mono_button = QPushButton("(Re)generate mono trades…", self)
        self._mono_button.clicked.connect(self._regenerate_mono_trades)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self._fetch_button)
        buttons_row.addWidget(self._mono_button)
        buttons_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._search_bar)
        layout.addWidget(self._table)
        layout.addLayout(buttons_row)
        layout.addWidget(self._status_label)

    def on_page_shown(self) -> None:
        self._search_bar.load(load_skins)

    def _on_results(self, rows: list[Skin]) -> None:
        self._model.set_rows(rows)

    def _on_load_failed(self, message: str) -> None:
        self._status_label.setText(f"Error loading skins: {message}")

    def _on_selection_changed(self, *_args) -> None:
        self._fetch_button.setEnabled(self._selected_skin() is not None)

    def _selected_skin(self) -> Skin | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.skin_at(indexes[0].row())

    def _fetch_prices(self) -> None:
        skin = self._selected_skin()
        if skin is None:
            return
        self._fetch_button.setEnabled(False)
        self._status_label.setText("Fetching…")
        worker = PriceFetchWorker(skin.id)
        keep_alive(self._inflight, worker)
        worker.signals.finished.connect(self._on_fetch_finished)
        worker.signals.error.connect(self._on_fetch_error)
        QThreadPool.globalInstance().start(worker)

    def _on_fetch_finished(self, payload: tuple[str, object]) -> None:
        skin_name, result = payload
        message = (
            f"Fetched {skin_name}: {result.observations} observations "
            f"({result.requests_made} requests, {result.wears_not_found} wears not found)"
        )
        if result.error:
            message += f" — stopped early: {result.error}"
        self._status_label.setText(message)
        self._fetch_button.setEnabled(True)
        self._search_bar.load(load_skins)  # refresh cached prices/timestamps for the row just fetched

    def _on_fetch_error(self, message: str) -> None:
        self._status_label.setText(f"Error: {message}")
        self._fetch_button.setEnabled(True)

    def _regenerate_mono_trades(self) -> None:
        max_price, ok = QInputDialog.getDouble(
            self, "Regenerate mono trades", "Max contract price ($):", 100.0, 0.0, 1_000_000.0, 2,
        )
        if not ok:
            return

        total = len(INPUT_RARITIES) * 2
        progress = QProgressDialog("Generating mono trades…", "Cancel", 0, total, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        worker = MonoTradeWorker(max_price)
        keep_alive(self._inflight, worker)
        worker.signals.progress.connect(lambda done, total_: self._on_mono_progress(progress, done, total_))
        worker.signals.finished.connect(lambda count: self._on_mono_finished(progress, count))
        worker.signals.error.connect(lambda message: self._on_mono_error(progress, message))
        QThreadPool.globalInstance().start(worker)

    def _on_mono_progress(self, progress: QProgressDialog, done: int, total: int) -> None:
        progress.setMaximum(total)
        progress.setValue(done)
        progress.setLabelText(f"Generating mono trades... ({done}/{total} combos)")

    def _on_mono_finished(self, progress: QProgressDialog, count: int) -> None:
        progress.close()
        QMessageBox.information(self, "Mono trades", f"Generated/updated {count} mono trade contract(s).")

    def _on_mono_error(self, progress: QProgressDialog, message: str) -> None:
        progress.close()
        QMessageBox.warning(self, "Mono trades", f"Failed to generate mono trades: {message}")
