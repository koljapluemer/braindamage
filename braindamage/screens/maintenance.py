"""Maintenance screen: the app's one admin action for now — fetch current
prices for a single skin via the CS2Cap API. Catalog import and bulk historical
import are throwaway scripts (see scripts/), not UI actions, since they're
infrequent one-off operations rather than something to run repeatedly.
"""

from __future__ import annotations

from sqlalchemy import select
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from .. import cs2cap_api
from ..db import SessionLocal
from ..models import Skin


class MaintenanceScreen(Screen):
    BINDINGS = [
        ("f", "fetch_prices", "Fetch prices for selected skin"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Input(placeholder="Search skins by name or collection...", id="search"),
            DataTable(id="skin_table"),
            Static("Select a skin and press 'f' to fetch its current prices.", id="status"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#skin_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Collection", "Rarity", "Variant", "Last Price", "Recalculated At")
        self._reload_skins()
        self.query_one("#search", Input).focus()

    def _reload_skins(self, query: str = "") -> None:
        table = self.query_one("#skin_table", DataTable)
        table.clear()
        with SessionLocal() as session:
            skins = list(session.scalars(select(Skin).order_by(Skin.name)))

        needle = query.strip().lower()
        for skin in skins:
            haystack = f"{skin.name} {skin.collection_name or ''}".lower()
            if needle and needle not in haystack:
                continue
            variant = "StatTrak" if skin.stattrak else "Souvenir" if skin.souvenir else "Normal"
            last_price = f"${skin.last_price:.2f}" if skin.last_price is not None else "—"
            recalculated = (
                skin.last_price_recalculated_at.strftime("%Y-%m-%d %H:%M")
                if skin.last_price_recalculated_at
                else "—"
            )
            table.add_row(
                skin.name, skin.collection_name or "—", skin.rarity_name or "—",
                variant, last_price, recalculated, key=skin.id,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._reload_skins(event.value)

    def action_fetch_prices(self) -> None:
        table = self.query_one("#skin_table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key.value is None:
            return
        self._fetch_prices(row_key.value)

    @work(thread=True, exclusive=True)
    def _fetch_prices(self, skin_id: str) -> None:
        def set_status(text: str) -> None:
            self.query_one("#status", Static).update(text)

        self.app.call_from_thread(set_status, "Fetching…")

        try:
            with SessionLocal() as session:
                skin = session.get(Skin, skin_id)
                if skin is None:
                    self.app.call_from_thread(set_status, "Skin not found.")
                    return
                skin_name = skin.name
                result = cs2cap_api.run_price_import(session, skin)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.app.call_from_thread(set_status, f"Error: {exc}")
            return

        message = (
            f"Fetched {skin_name}: {result.observations} observations "
            f"({result.requests_made} requests, {result.wears_not_found} wears not found)"
        )
        if result.error:
            message += f" — stopped early: {result.error}"

        def finish() -> None:
            set_status(message)
            self._reload_skins(self.query_one("#search", Input).value)

        self.app.call_from_thread(finish)
