"""Historical contract detail dialog -- port of the Textual ContractDetailModal.
Reuses LineTableModel/OutcomeTableModel/ResultSummaryPanel so its rendering
(including CVaR display) matches the live builder view exactly.

Also owns three recalculation actions: refetch prices for every input/output
skin via either Steam's priceoverview endpoint or the CS2Cap API (the
Maintenance page's single-skin fetch button), or just re-simulate against
whatever prices are already on disk -- then re-display this same contract.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from ... import contracts as contracts_module
from ...db import SessionLocal
from ...models import Contract
from ..models.line_table_model import LineTableModel
from ..models.outcome_table_model import OutcomeTableModel
from ..widgets.ev_curve_chart import EvCurveChart
from ..widgets.result_summary_panel import ResultSummaryPanel
from ..workers.cs2cap_contract_price_worker import Cs2capContractPriceWorker
from ..workers.signals import keep_alive
from ..workers.steam_contract_price_worker import SteamContractPriceWorker


class ContractDetailDialog(QDialog):
    def __init__(self, contract: Contract, parent=None) -> None:
        super().__init__(parent)
        self._inflight: list = []
        self._contract_id = contract.id
        self._progress: QProgressDialog | None = None

        self.resize(800, 850)

        self._summary = ResultSummaryPanel(self)
        self._ev_curve_chart = EvCurveChart(self)

        self._line_model = LineTableModel(self)
        line_view = QTableView(self)
        line_view.setModel(self._line_model)
        line_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        line_view.horizontalHeader().setStretchLastSection(True)

        self._outcome_model = OutcomeTableModel(self)
        outcome_view = QTableView(self)
        outcome_view.setModel(self._outcome_model)
        outcome_view.setSortingEnabled(True)
        outcome_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        outcome_view.horizontalHeader().setStretchLastSection(True)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)

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
        self._fetch_buttons = (self._steam_button, self._cs2cap_button, self._recalculate_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.accept)

        fetch_row = QHBoxLayout()
        fetch_row.addWidget(self._steam_button)
        fetch_row.addWidget(self._cs2cap_button)
        fetch_row.addWidget(self._recalculate_button)
        fetch_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(QLabel("Inputs:"))
        layout.addWidget(line_view)
        layout.addWidget(QLabel("Outcomes:"))
        layout.addWidget(outcome_view)
        layout.addWidget(self._ev_curve_chart)
        layout.addLayout(fetch_row)
        layout.addWidget(self._status_label)
        layout.addWidget(buttons)

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
        self._ev_curve_chart.set_points(contract.ev_curve)

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
