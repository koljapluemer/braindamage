"""Contracts page: every simulated trade-up contract, favorited ones first by
default (now re-sortable by clicking any column header), with favorite
toggling and a detail drill-down. Port of the Textual ContractsScreen.
"""

from __future__ import annotations

from sqlalchemy import select

from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ... import contracts as contracts_module
from ...db import SessionLocal
from ...models import Contract
from ..dialogs.contract_detail_dialog import ContractDetailDialog
from ..models.contract_table_model import ContractTableModel

_MAX_COST_LIMIT = 1_000_000.0


def _load_contracts() -> list[Contract]:
    with SessionLocal() as session:
        return list(session.scalars(select(Contract)))


class ContractsPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._all_contracts: list[Contract] = []

        self._hide_bad_checkbox = QCheckBox("Hide bad trades (negative EV)", self)
        self._hide_bad_checkbox.setChecked(True)
        self._hide_bad_checkbox.toggled.connect(self._apply_filters)

        self._hide_uncalculable_checkbox = QCheckBox("Hide uncalculable trades (incomplete data)", self)
        self._hide_uncalculable_checkbox.setChecked(True)
        self._hide_uncalculable_checkbox.toggled.connect(self._apply_filters)

        self._max_cost_spin = QDoubleSpinBox(self)
        self._max_cost_spin.setDecimals(2)
        self._max_cost_spin.setRange(0.0, _MAX_COST_LIMIT)
        self._max_cost_spin.setSingleStep(1.0)
        self._max_cost_spin.setPrefix("$")
        self._max_cost_spin.setSpecialValueText("$0 (no limit)")
        self._max_cost_spin.setValue(0.0)
        self._max_cost_spin.valueChanged.connect(self._apply_filters)

        filter_row = QHBoxLayout()
        filter_row.addWidget(self._hide_bad_checkbox)
        filter_row.addWidget(self._hide_uncalculable_checkbox)
        filter_row.addWidget(QLabel("Max contract cost:", self))
        filter_row.addWidget(self._max_cost_spin)
        filter_row.addStretch(1)

        self._model = ContractTableModel(self)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(lambda _index: self._show_detail())
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        self._status_label = QLabel("", self)

        self._favorite_button = QPushButton("Toggle favorite", self)
        self._favorite_button.setEnabled(False)
        self._favorite_button.clicked.connect(lambda: self._toggle_favorite())

        self._detail_button = QPushButton("View details", self)
        self._detail_button.setEnabled(False)
        self._detail_button.clicked.connect(lambda: self._show_detail())

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self._favorite_button)
        buttons_row.addWidget(self._detail_button)
        buttons_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_row)
        layout.addWidget(self._status_label)
        layout.addWidget(self._table)
        layout.addLayout(buttons_row)

    def on_page_shown(self) -> None:
        self._reload()

    def _reload(self) -> None:
        self._all_contracts = _load_contracts()
        self._apply_filters()

    def _apply_filters(self, *_args) -> None:
        rows = contracts_module.filter_contracts(
            self._all_contracts,
            hide_bad_trades=self._hide_bad_checkbox.isChecked(),
            hide_uncalculable_trades=self._hide_uncalculable_checkbox.isChecked(),
            max_cost=self._max_cost_spin.value(),
        )
        self._model.set_rows(rows)
        favorited = sum(1 for row in rows if row.favorite)
        self._status_label.setText(
            f"{len(rows)} of {len(self._all_contracts)} contracts shown, {favorited} favorited."
        )

    def _selected_contract(self) -> Contract | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.contract_at(indexes[0].row())

    def _on_selection_changed(self, *_args) -> None:
        has_selection = self._selected_contract() is not None
        self._favorite_button.setEnabled(has_selection)
        self._detail_button.setEnabled(has_selection)

    def _toggle_favorite(self) -> None:
        contract = self._selected_contract()
        if contract is None:
            return
        with SessionLocal() as session:
            contracts_module.set_favorite(session, contract.id, not contract.favorite)
        contract.favorite = not contract.favorite
        selected_id = contract.id
        self._apply_filters()
        self._restore_selection(selected_id)

    def _restore_selection(self, contract_id: str) -> None:
        for row in range(self._model.rowCount()):
            contract = self._model.contract_at(row)
            if contract is not None and contract.id == contract_id:
                self._table.selectRow(row)
                self._table.setFocus()
                break

    def _show_detail(self) -> None:
        contract = self._selected_contract()
        if contract is None:
            return
        dialog = ContractDetailDialog(contract, self)
        dialog.favoriteChanged.connect(self._on_dialog_favorite_changed)
        dialog.exec()

    def _on_dialog_favorite_changed(self, contract_id: str, favorite: bool) -> None:
        for contract in self._all_contracts:
            if contract.id == contract_id:
                contract.favorite = favorite
                break
        self._apply_filters()
        self._restore_selection(contract_id)
