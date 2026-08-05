"""Contracts screen: every simulated trade-up contract, favorited ones first,
with favorite toggling and a detail drill-down.
"""

from __future__ import annotations

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import contracts as contracts_module
from ..db import SessionLocal
from ..models import Contract


class ContractDetailModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss_modal", "Close"), ("enter", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    ContractDetailModal {
        align: center middle;
    }
    ContractDetailModal > Vertical {
        width: 90%;
        height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ContractDetailModal DataTable {
        height: 1fr;
    }
    """

    def __init__(self, contract: Contract) -> None:
        super().__init__()
        self._contract = contract

    def compose(self) -> ComposeResult:
        c = self._contract
        roi = f"{c.roi:.1%}" if c.roi is not None else "—"
        cvar = f"${c.cvar_5pct:.2f}" if c.cvar_5pct is not None else "—"
        with Vertical():
            yield Static(
                f"{c.rarity_name} → {c.target_rarity_name}   "
                f"{'StatTrak' if c.stattrak else 'Normal'}   "
                f"Input cost: ${c.input_cost:.2f}   EV: ${c.expected_value:+.2f}   "
                f"ROI: {roi}   CVaR(5%): {cvar}"
            )
            yield Static("Inputs:")
            input_table = DataTable(id="input_lines_table")
            input_table.add_columns("Skin", "Collection", "Float", "Qty")
            for line in c.input_lines:
                input_table.add_row(
                    line["skin_name"], line["collection_name"],
                    f"{line['float_value']:.4f}", str(line["quantity"]),
                )
            yield input_table
            yield Static("Outcomes:")
            outcomes_table = DataTable(id="outcomes_detail_table")
            outcomes_table.add_columns("Probability", "Skin", "Wear", "Net Price", "Contribution")
            for outcome in c.outcomes:
                net_price = f"${outcome['net_price']:.2f}" if outcome.get("net_price") is not None else "—"
                outcomes_table.add_row(
                    f"{outcome['probability']:.2%}", outcome["skin_name"], outcome["predicted_wear"],
                    net_price, f"${outcome['contribution']:.2f}",
                )
            yield outcomes_table

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class ContractsScreen(Screen):
    BINDINGS = [
        ("f", "toggle_favorite", "Toggle favorite"),
        ("enter", "show_detail", "View details"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("", id="contracts_status"),
            DataTable(id="contracts_table"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#contracts_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Fav", "Rarity", "Variant", "Input Cost", "EV", "ROI", "CVaR(5%)", "Last Simulated")
        self._reload()

    def _reload(self) -> None:
        table = self.query_one("#contracts_table", DataTable)
        table.clear()
        with SessionLocal() as session:
            rows = list(
                session.scalars(
                    select(Contract).order_by(Contract.favorite.desc(), Contract.expected_value.desc())
                )
            )

        for row in rows:
            roi = f"{row.roi:.1%}" if row.roi is not None else "—"
            cvar = f"${row.cvar_5pct:.2f}" if row.cvar_5pct is not None else "—"
            variant = "StatTrak" if row.stattrak else "Normal"
            table.add_row(
                "★" if row.favorite else "☆",
                row.rarity_name, variant,
                f"${row.input_cost:.2f}", f"${row.expected_value:+.2f}", roi, cvar,
                row.last_simulated_at.strftime("%Y-%m-%d %H:%M"),
                key=row.id,
            )

        favorited_count = sum(1 for row in rows if row.favorite)
        self.query_one("#contracts_status", Static).update(
            f"{len(rows)} contracts total, {favorited_count} favorited. "
            "'f' toggles favorite, Enter views details."
        )

    def _selected_id(self) -> str | None:
        table = self.query_one("#contracts_table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return row_key.value

    def action_toggle_favorite(self) -> None:
        contract_id = self._selected_id()
        if contract_id is None:
            return
        with SessionLocal() as session:
            row = session.get(Contract, contract_id)
            if row is None:
                return
            contracts_module.set_favorite(session, contract_id, not row.favorite)
        self._reload()

    def action_show_detail(self) -> None:
        contract_id = self._selected_id()
        if contract_id is None:
            return
        with SessionLocal() as session:
            row = session.get(Contract, contract_id)
        if row is None:
            return
        self.app.push_screen(ContractDetailModal(row))
