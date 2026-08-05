"""Contract builder page -- the redesign target.

Two-pane QSplitter layout (lines/controls on the left, live results on the
right) replaces the Textual version's single vertical stack, which forced the
outcomes table below several stacked summary widgets. Also restores CVaR to
the live result summary (previously only shown on the historical Contracts
detail view, despite always being computed) via the shared ResultSummaryPanel,
and uses the skin's real rarity_color for the rarity badge.

State discipline mirrors the original ContractBuilderScreen: all mutation
goes through ContractBuilderController, whose `stateChanged` signal is the one
place this page re-renders from.
"""

from __future__ import annotations

import dataclasses

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ... import tradeup
from ...tradeup import RARITY_LADDER
from ..dialogs.line_detail_dialog import LineDetailDialog
from ..dialogs.skin_picker_dialog import SkinPickerDialog
from ..models.line_table_model import LineTableModel, line_to_row
from ..models.outcome_table_model import OutcomeTableModel
from ..widgets.rarity_badge import RarityBadge
from ..widgets.result_summary_panel import ResultSummaryPanel
from .contract_builder_controller import ContractBuilderController

_RARITY_COLORS = dict(RARITY_LADDER)


class ContractBuilderPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._controller = ContractBuilderController(self)
        self._controller.stateChanged.connect(self._render)
        self._controller.statusChanged.connect(self._set_status)

        # --- Left pane: contract state header + lines ---
        self._rarity_badge = RarityBadge(self)
        self._variant_label = QLabel(self)
        self._quantity_label = QLabel(self)
        header_row = QHBoxLayout()
        header_row.addWidget(self._rarity_badge)
        header_row.addWidget(self._variant_label)
        header_row.addWidget(self._quantity_label)
        header_row.addStretch(1)

        self._lines_model = LineTableModel(self)
        self._lines_table = QTableView(self)
        self._lines_table.setModel(self._lines_model)
        self._lines_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._lines_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._lines_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._lines_table.horizontalHeader().setStretchLastSection(True)
        self._lines_table.selectionModel().selectionChanged.connect(self._on_line_selection_changed)

        self._add_button = QPushButton("Add input", self)
        self._add_button.clicked.connect(lambda: self._add_line())
        self._edit_button = QPushButton("Edit selected", self)
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(lambda: self._edit_line())
        self._delete_button = QPushButton("Delete selected", self)
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(lambda: self._delete_line())
        self._new_button = QPushButton("New contract", self)
        self._new_button.clicked.connect(lambda: self._controller.new_contract())

        line_buttons_row = QHBoxLayout()
        line_buttons_row.addWidget(self._add_button)
        line_buttons_row.addWidget(self._edit_button)
        line_buttons_row.addWidget(self._delete_button)
        line_buttons_row.addWidget(self._new_button)
        line_buttons_row.addStretch(1)

        self._status_label = QLabel(self)
        self._status_label.setWordWrap(True)

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.addLayout(header_row)
        left_layout.addWidget(self._lines_table)
        left_layout.addLayout(line_buttons_row)
        left_layout.addWidget(self._status_label)

        # --- Right pane: live results ---
        self._summary = ResultSummaryPanel(self)

        self._run_button = QPushButton("Run simulation", self)
        self._run_button.setEnabled(False)
        self._run_button.clicked.connect(lambda: self._controller.run_simulation())
        self._favorite_button = QPushButton("Toggle favorite", self)
        self._favorite_button.setEnabled(False)
        self._favorite_button.clicked.connect(lambda: self._controller.toggle_favorite())

        result_buttons_row = QHBoxLayout()
        result_buttons_row.addWidget(self._run_button)
        result_buttons_row.addWidget(self._favorite_button)
        result_buttons_row.addStretch(1)

        self._outcomes_model = OutcomeTableModel(self)
        self._outcomes_table = QTableView(self)
        self._outcomes_table.setModel(self._outcomes_model)
        self._outcomes_table.setSortingEnabled(True)
        self._outcomes_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._outcomes_table.horizontalHeader().setStretchLastSection(True)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self._summary)
        right_layout.addLayout(result_buttons_row)
        right_layout.addWidget(self._outcomes_table)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        self._render()
        self._set_status("Click 'Add input' to add an input. Needs exactly 10 quantity before it can be simulated.")

    # Deliberately no on_page_shown() here -- unlike the other three pages,
    # this one's in-progress state must survive navigating away and back.

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _selected_index(self) -> int | None:
        indexes = self._lines_table.selectionModel().selectedRows()
        return indexes[0].row() if indexes else None

    def _on_line_selection_changed(self, *_args) -> None:
        has_selection = self._selected_index() is not None
        self._edit_button.setEnabled(has_selection)
        self._delete_button.setEnabled(has_selection)

    def _add_line(self) -> None:
        remaining = 10 - self._controller.total_quantity
        if remaining <= 0:
            self._set_status("Contract already has 10 inputs — remove one first, or click 'New contract'.")
            return

        options = self._controller.eligible_options()
        picker = SkinPickerDialog(options, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        option = picker.picked_option()
        if option is None:
            return

        detail = LineDetailDialog(option, max_quantity=remaining, parent=self)
        if detail.exec() != QDialog.DialogCode.Accepted:
            return
        result = detail.result_value()
        if result is None:
            return
        float_value, quantity = result
        self._controller.add_line(option, float_value, quantity)

    def _edit_line(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        option = self._controller.option_for_existing_line(index)
        if option is None:
            return

        line = self._controller.lines[index]
        remaining = 10 - self._controller.total_quantity + line.quantity
        detail = LineDetailDialog(
            option,
            max_quantity=remaining,
            initial_float=line.float_value,
            initial_quantity=line.quantity,
            parent=self,
        )
        if detail.exec() != QDialog.DialogCode.Accepted:
            return
        result = detail.result_value()
        if result is None:
            return
        float_value, quantity = result
        self._controller.edit_line(index, float_value, quantity)

    def _delete_line(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        self._controller.delete_line(index)

    def _render(self) -> None:
        controller = self._controller

        self._rarity_badge.set_rarity(controller.rarity_name, _RARITY_COLORS.get(controller.rarity_name))
        if controller.stattrak is None:
            self._variant_label.setText("")
        else:
            self._variant_label.setText("StatTrak" if controller.stattrak else "Normal")
        self._quantity_label.setText(f"Quantity: {controller.total_quantity}/10")

        self._lines_model.set_rows([line_to_row(line) for line in controller.lines])
        self._add_button.setEnabled(controller.total_quantity < 10)
        self._run_button.setEnabled(controller.total_quantity == 10)
        self._favorite_button.setEnabled(controller.last_result is not None)

        if controller.last_result is None:
            self._summary.clear()
            self._outcomes_model.set_rows([])
            return

        result = controller.last_result
        cvar_5pct = tradeup.cvar(tradeup.outcome_profits(result), alpha=0.05)
        self._summary.set_result(
            input_cost=result.input_cost,
            expected_value=result.expected_value,
            roi=result.roi,
            cvar_5pct=cvar_5pct,
            favorite=controller.last_favorite,
            missing_input_count=len(result.missing_input_price_names),
            missing_output_count=len(result.missing_output_price_names),
        )
        self._outcomes_model.set_rows([dataclasses.asdict(o) for o in result.outcomes])
