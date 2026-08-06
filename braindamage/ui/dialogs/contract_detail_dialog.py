"""Historical contract detail dialog -- port of the Textual ContractDetailModal.
Reuses LineTableModel/OutcomeTableModel/ResultSummaryPanel so its rendering
(including CVaR display) matches the live builder view exactly.

Also owns three recalculation actions: refetch prices for every input/output
skin via either Steam's priceoverview endpoint or the CS2Cap API (the
Maintenance page's single-skin fetch button), or just re-simulate against
whatever prices are already on disk -- then re-display this same contract.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ... import contracts as contracts_module
from ...db import SessionLocal
from ...models import Contract, Skin
from ...signals import now_utc
from ..models.line_table_model import LineTableModel
from ..models.outcome_table_model import OutcomeTableModel
from ..models.optimization_range_table_model import OptimizationRangeTableModel
from ..widgets.ev_curve_chart import EvCurveChart
from ..widgets.result_summary_panel import ResultSummaryPanel
from ..workers.cs2cap_contract_price_worker import Cs2capContractPriceWorker
from ..workers.signals import keep_alive
from ..workers.steam_contract_price_worker import SteamContractPriceWorker


class ContractDetailDialog(QDialog):
    favoriteChanged = Signal(str, bool)

    @staticmethod
    def _age(value) -> str:
        seconds = max(0, int((now_utc() - value).total_seconds()))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def __init__(self, contract: Contract, parent=None) -> None:
        super().__init__(parent)
        self._inflight: list = []
        self._contract_id = contract.id
        self._progress: QProgressDialog | None = None
        self._favorite = contract.favorite

        self.resize(800, 850)

        self._summary = ResultSummaryPanel(self)
        self._ev_curve_chart = EvCurveChart(self)

        self._line_model = LineTableModel(self)
        line_view = QTableView(self)
        line_view.setModel(self._line_model)
        line_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        line_view.horizontalHeader().setStretchLastSection(True)
        line_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        line_view.setColumnWidth(0, 280)

        self._outcome_model = OutcomeTableModel(self)
        outcome_view = QTableView(self)
        outcome_view.setModel(self._outcome_model)
        outcome_view.setSortingEnabled(True)
        outcome_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        outcome_view.horizontalHeader().setStretchLastSection(True)
        outcome_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        outcome_view.setColumnWidth(1, 280)
        outcome_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outcome_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._outcome_view = outcome_view

        self._range_model = OptimizationRangeTableModel(self)
        range_view = QTableView(self)
        range_view.setModel(self._range_model)
        range_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        range_view.setMaximumHeight(140)
        range_view.horizontalHeader().setStretchLastSection(True)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        self._freshness_label = QLabel("", self)

        self._steam_button = QPushButton("Fetch prices from Steam and recalculate", self)
        self._steam_button.clicked.connect(
            lambda: self._start_fetch(SteamContractPriceWorker(self._contract_id), "Steam")
        )

        self._cs2cap_button = QPushButton("Fetch prices from CS2Cap and recalculate", self)
        self._cs2cap_button.clicked.connect(
            lambda: self._start_fetch(Cs2capContractPriceWorker(self._contract_id), "CS2Cap")
        )
        self._recalculate_button = QPushButton("Recalculate (no price fetch)", self)
        self._recalculate_button.clicked.connect(self._recalculate)
        self._favorite_button = QPushButton(self)
        self._favorite_button.clicked.connect(self._toggle_favorite)
        self._fetch_buttons = (self._steam_button, self._cs2cap_button, self._recalculate_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.accept)

        fetch_row = QHBoxLayout()
        fetch_row.addWidget(self._steam_button)
        fetch_row.addWidget(self._cs2cap_button)
        fetch_row.addWidget(self._recalculate_button)
        fetch_row.addWidget(self._favorite_button)
        fetch_row.addStretch(1)

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.addWidget(self._summary)
        layout.addWidget(QLabel("Inputs:"))
        layout.addWidget(line_view)
        layout.addWidget(QLabel("Outcomes:"))
        layout.addWidget(outcome_view)
        layout.addWidget(self._ev_curve_chart)
        layout.addWidget(QLabel("Best input-float buying ranges:"))
        layout.addWidget(range_view)
        layout.addLayout(fetch_row)
        layout.addWidget(self._freshness_label)
        layout.addWidget(self._status_label)
        layout.addWidget(buttons)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)

        self._render(contract)

    def _render(self, contract: Contract) -> None:
        variant = "StatTrak" if contract.stattrak else "Normal"
        self.setWindowTitle(f"{contract.rarity_name} → {contract.target_rarity_name} ({variant})")
        self._summary.set_result(
            input_cost=contract.input_cost,
            expected_value=contract.expected_value,
            roi=contract.roi,
            cvar_5pct=contract.cvar_5pct,
            favorite=contract.favorite,
            missing_input_count=len(contract.missing_input_price_names),
            missing_output_count=len(contract.missing_output_price_names),
        )
        self._line_model.set_rows(contract.input_lines)
        self._outcome_model.set_rows(contract.outcomes)
        self._outcome_view.resizeRowsToContents()
        header_height = self._outcome_view.horizontalHeader().height()
        rows_height = sum(self._outcome_view.rowHeight(i) for i in range(self._outcome_model.rowCount()))
        self._outcome_view.setFixedHeight(header_height + rows_height + 4)
        annotations = getattr(contract, "ev_curve_annotations", []) or []
        chart_points = [dict(point, **annotations[i]) if i < len(annotations) else point for i, point in enumerate(contract.ev_curve)]
        self._ev_curve_chart.set_points(chart_points)
        self._range_model.set_rows(getattr(contract, "optimization_ranges", []) or [])
        self._favorite = contract.favorite
        self._favorite_button.setText("Unfavorite" if self._favorite else "Favorite")
        timestamps = [contract.last_simulated_at]
        with SessionLocal() as session:
            for skin_id in contracts_module.referenced_skin_ids(contract):
                skin = session.get(Skin, skin_id)
                if skin is not None and skin.last_price_calculation_data_point_recency is not None:
                    timestamps.append(skin.last_price_calculation_data_point_recency)
        oldest = min(timestamps[1:]) if len(timestamps) > 1 else None
        refreshed = f"{contract.last_simulated_at:%Y-%m-%d %H:%M} ({self._age(contract.last_simulated_at)})"
        oldest_text = f"{oldest:%Y-%m-%d %H:%M} ({self._age(oldest)})" if oldest else "unknown"
        self._freshness_label.setText(f"Last refreshed: {refreshed} · oldest underlying price: {oldest_text}")

    def _toggle_favorite(self) -> None:
        self._favorite = not self._favorite
        with SessionLocal() as session:
            contracts_module.set_favorite(session, self._contract_id, self._favorite)
        self._favorite_button.setText("Unfavorite" if self._favorite else "Favorite")
        self.favoriteChanged.emit(self._contract_id, self._favorite)

    def _recalculate(self) -> None:
        """Re-simulates against whatever prices are already on disk -- no
        network call, unlike the two fetch buttons, so it runs synchronously
        on the UI thread."""
        for button in self._fetch_buttons:
            button.setEnabled(False)
        self._status_label.setText("")
        try:
            with SessionLocal() as session:
                contract = session.get(Contract, self._contract_id)
                if contract is None:
                    return
                contract = contracts_module.resimulate(session, contract)
                self._render(contract)
            self._status_label.setText("Recalculated from existing price data.")
        finally:
            for button in self._fetch_buttons:
                button.setEnabled(True)

    def _start_fetch(self, worker, source_label: str) -> None:
        for button in self._fetch_buttons:
            button.setEnabled(False)
        self._status_label.setText("")

        progress = QProgressDialog(f"Fetching {source_label} prices…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)  # not wired to actually stop the worker -- omit rather than mislead
        progress.setValue(0)
        self._progress = progress

        keep_alive(self._inflight, worker)
        worker.signals.progress.connect(lambda done, total: self._on_fetch_progress(source_label, done, total))
        worker.signals.finished.connect(self._on_fetch_finished)
        worker.signals.error.connect(lambda message: self._on_fetch_error(source_label, message))
        QThreadPool.globalInstance().start(worker)

    def _on_fetch_progress(self, source_label: str, done: int, total: int) -> None:
        if self._progress is None:
            return
        self._progress.setMaximum(total)
        self._progress.setValue(done)
        self._progress.setLabelText(f"Fetching {source_label} prices… ({done}/{total})")

    def _on_fetch_finished(self, result: object) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None

        with SessionLocal() as session:
            contract = session.get(Contract, self._contract_id)
            if contract is not None:
                self._render(contract)

        message = (
            f"Updated {result.skins_updated} skin(s): {result.observations} observations "
            f"({result.requests_made} requests, {result.wears_not_found} wears not found)"
        )
        if result.error:
            message += f" — stopped early: {result.error}"
        self._status_label.setText(message)
        for button in self._fetch_buttons:
            button.setEnabled(True)

    def _on_fetch_error(self, source_label: str, message: str) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        for button in self._fetch_buttons:
            button.setEnabled(True)
        QMessageBox.warning(self, f"Fetch prices from {source_label}", f"Failed to fetch prices: {message}")
