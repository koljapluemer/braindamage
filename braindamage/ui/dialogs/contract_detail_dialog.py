"""Historical contract detail dialog -- port of the Textual ContractDetailModal.
Reuses LineTableModel/OutcomeTableModel/ResultSummaryPanel so its rendering
(including CVaR display) matches the live builder view exactly.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTableView, QVBoxLayout

from ...models import Contract
from ..models.line_table_model import LineTableModel
from ..models.outcome_table_model import OutcomeTableModel
from ..widgets.result_summary_panel import ResultSummaryPanel


class ContractDetailDialog(QDialog):
    def __init__(self, contract: Contract, parent=None) -> None:
        super().__init__(parent)
        self.resize(800, 600)
        variant = "StatTrak" if contract.stattrak else "Normal"
        self.setWindowTitle(f"{contract.rarity_name} → {contract.target_rarity_name} ({variant})")

        summary = ResultSummaryPanel(self)
        summary.set_result(
            input_cost=contract.input_cost,
            expected_value=contract.expected_value,
            roi=contract.roi,
            cvar_5pct=contract.cvar_5pct,
            favorite=contract.favorite,
            missing_input_count=len(contract.missing_input_price_names),
            missing_output_count=len(contract.missing_output_price_names),
        )

        line_model = LineTableModel(self)
        line_model.set_rows(contract.input_lines)
        line_view = QTableView(self)
        line_view.setModel(line_model)
        line_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        line_view.horizontalHeader().setStretchLastSection(True)

        outcome_model = OutcomeTableModel(self)
        outcome_model.set_rows(contract.outcomes)
        outcome_view = QTableView(self)
        outcome_view.setModel(outcome_model)
        outcome_view.setSortingEnabled(True)
        outcome_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        outcome_view.horizontalHeader().setStretchLastSection(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(QLabel("Inputs:"))
        layout.addWidget(line_view)
        layout.addWidget(QLabel("Outcomes:"))
        layout.addWidget(outcome_view)
        layout.addWidget(buttons)
