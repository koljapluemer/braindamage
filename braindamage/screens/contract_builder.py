"""Contract builder screen: assemble a 10-input trade-up contract and simulate
it. Adding/editing an input goes through the search-first modals in
contract_modals.py rather than inline table editing — keeps the state machine
simple (the contract's lines only ever change via one well-defined dialog
result, never a half-edited table cell) per this project's UI conventions.
"""

from __future__ import annotations

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import contracts, tradeup
from ..db import SessionLocal
from ..models import Skin
from ..tradeup import ContractLine, ContractState, SimulationResult, SkinOption
from .contract_modals import LineDetailModal, SkinPickerModal


class ContractBuilderScreen(Screen):
    BINDINGS = [
        ("a", "add_skin", "Add input"),
        ("e", "edit_line", "Edit selected"),
        ("d", "delete_line", "Delete selected"),
        ("r", "run_simulation", "Run simulation"),
        ("f", "toggle_favorite", "Toggle favorite"),
        ("n", "new_contract", "New contract"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[ContractLine] = []
        self.rarity_name: str | None = None
        self.stattrak: bool | None = None
        self.last_result: SimulationResult | None = None
        self.last_contract_id: str | None = None
        self.last_favorite: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("", id="contract_state"),
            DataTable(id="lines_table"),
            Static("", id="builder_status"),
            Static("", id="result_summary"),
            DataTable(id="outcomes_table"),
        )
        yield Footer()

    def on_mount(self) -> None:
        lines_table = self.query_one("#lines_table", DataTable)
        lines_table.cursor_type = "row"
        lines_table.add_columns("Skin", "Collection", "Wear", "Float", "Qty")

        outcomes_table = self.query_one("#outcomes_table", DataTable)
        outcomes_table.cursor_type = "row"
        outcomes_table.add_columns("Probability", "Skin", "Wear", "Net Price", "Contribution")

        self._refresh_lines_table()
        self._set_status("Press 'a' to add an input. Needs exactly 10 quantity before it can be simulated.")

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    def _set_status(self, message: str) -> None:
        self.query_one("#builder_status", Static).update(message)

    def _refresh_lines_table(self) -> None:
        table = self.query_one("#lines_table", DataTable)
        table.clear()
        for index, line in enumerate(self.lines):
            wear = tradeup.wear_for_float(line.float_value)
            table.add_row(
                line.skin_name, line.collection_name, wear,
                f"{line.float_value:.4f}", str(line.quantity), key=str(index),
            )

        rarity = self.rarity_name or "—"
        stattrak = "—" if self.stattrak is None else ("StatTrak" if self.stattrak else "Normal")
        self.query_one("#contract_state", Static).update(
            f"Rarity: {rarity}   Variant: {stattrak}   Quantity: {self.total_quantity}/10"
        )

    def _selected_index(self) -> int | None:
        table = self.query_one("#lines_table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return int(row_key.value) if row_key.value is not None else None

    # --- Add / edit / delete ---------------------------------------------------

    def action_add_skin(self) -> None:
        remaining = 10 - self.total_quantity
        if remaining <= 0:
            self._set_status("Contract already has 10 inputs — remove one first, or press 'n' to start over.")
            return

        with SessionLocal() as session:
            options = tradeup.eligible_input_options(session)
        if self.rarity_name is not None:
            options = [o for o in options if o.rarity_name == self.rarity_name and o.stattrak == self.stattrak]

        self.app.push_screen(SkinPickerModal(options), self._on_skin_picked)

    def _on_skin_picked(self, option: SkinOption | None) -> None:
        if option is None:
            return
        remaining = 10 - self.total_quantity
        self.app.push_screen(
            LineDetailModal(option, max_quantity=remaining),
            lambda result: self._on_line_added(option, result),
        )

    def _on_line_added(self, option: SkinOption, result: tuple[float, int] | None) -> None:
        if result is None:
            return
        float_value, quantity = result
        if self.rarity_name is None:
            self.rarity_name = option.rarity_name
            self.stattrak = option.stattrak
        self.lines.append(
            ContractLine(
                skin_id=option.skin_id,
                skin_name=option.skin_name,
                collection_id=option.collection_id,
                collection_name=option.collection_name,
                float_value=float_value,
                quantity=quantity,
            )
        )
        self._refresh_lines_table()
        self._set_status(f"Added {option.skin_name}.")

    def action_edit_line(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        line = self.lines[index]
        with SessionLocal() as session:
            skin = session.get(Skin, line.skin_id)
        if skin is None:
            self._set_status(f"{line.skin_name} no longer exists in the catalog.")
            return
        option = SkinOption(
            skin_id=skin.id,
            skin_name=skin.name,
            collection_id=skin.collection_id,
            collection_name=skin.collection_name,
            rarity_name=skin.rarity_name,
            stattrak=skin.stattrak,
            min_float=skin.min_float if skin.min_float is not None else 0.0,
            max_float=skin.max_float if skin.max_float is not None else 1.0,
        )
        remaining = 10 - self.total_quantity + line.quantity
        self.app.push_screen(
            LineDetailModal(
                option, max_quantity=remaining,
                initial_float=line.float_value, initial_quantity=line.quantity,
            ),
            lambda result: self._on_line_edited(index, result),
        )

    def _on_line_edited(self, index: int, result: tuple[float, int] | None) -> None:
        if result is None:
            return
        float_value, quantity = result
        self.lines[index].float_value = float_value
        self.lines[index].quantity = quantity
        self._refresh_lines_table()

    def action_delete_line(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        removed = self.lines.pop(index)
        if not self.lines:
            self.rarity_name = None
            self.stattrak = None
        self._refresh_lines_table()
        self._set_status(f"Removed {removed.skin_name}.")

    def action_new_contract(self) -> None:
        self.lines = []
        self.rarity_name = None
        self.stattrak = None
        self.last_result = None
        self.last_contract_id = None
        self.last_favorite = False
        self._refresh_lines_table()
        self.query_one("#result_summary", Static).update("")
        self.query_one("#outcomes_table", DataTable).clear()
        self._set_status("Started a new contract. Press 'a' to add an input.")

    # --- Simulation --------------------------------------------------------------

    def action_run_simulation(self) -> None:
        if self.total_quantity != 10 or self.rarity_name is None:
            self._set_status(f"Need exactly 10 inputs to simulate (currently {self.total_quantity}/10).")
            return

        contract = ContractState(rarity_name=self.rarity_name, stattrak=bool(self.stattrak), lines=list(self.lines))
        with SessionLocal() as session:
            result = tradeup.simulate_contract(session, contract)
            row = contracts.upsert_contract(session, contract, result)
            self.last_contract_id = row.id
            self.last_favorite = row.favorite

        self.last_result = result
        self._render_results()
        self._set_status("Simulation complete — press 'f' to favorite this contract.")

    def _render_results(self) -> None:
        result = self.last_result
        if result is None:
            return

        favorite_marker = "★ favorited" if self.last_favorite else "☆ not favorited"
        roi = f"{result.roi:.1%}" if result.roi is not None else "—"
        summary = (
            f"Input cost: ${result.input_cost:.2f}   "
            f"Expected value: ${result.expected_value:+.2f}   "
            f"ROI: {roi}   "
            f"[{favorite_marker}]"
        )
        if result.missing_input_price_names or result.missing_output_price_names:
            summary += f"   (missing prices: {len(result.missing_input_price_names)} input, {len(result.missing_output_price_names)} output)"
        self.query_one("#result_summary", Static).update(summary)

        outcomes_table = self.query_one("#outcomes_table", DataTable)
        outcomes_table.clear()
        for outcome in result.outcomes:
            net_price = f"${outcome.net_price:.2f}" if outcome.net_price is not None else "—"
            outcomes_table.add_row(
                f"{outcome.probability:.2%}", outcome.skin_name, outcome.predicted_wear,
                net_price, f"${outcome.contribution:.2f}",
            )

    def action_toggle_favorite(self) -> None:
        if self.last_contract_id is None:
            self._set_status("Run a simulation before favoriting it.")
            return
        with SessionLocal() as session:
            contracts.set_favorite(session, self.last_contract_id, not self.last_favorite)
        self.last_favorite = not self.last_favorite
        self._render_results()
