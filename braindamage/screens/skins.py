"""Skins screen: browse every catalogued skin and its last known price."""

from __future__ import annotations

from sqlalchemy import select
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from ..db import SessionLocal
from ..models import Skin


class SkinsScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Input(placeholder="Filter by name or collection...", id="search"),
            DataTable(id="skin_table"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#skin_table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Name", "Collection", "Rarity", "Variant",
            "Last Price", "Data Recency", "Recalculated At",
        )
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
            recency = (
                skin.last_price_calculation_data_point_recency.strftime("%Y-%m-%d %H:%M")
                if skin.last_price_calculation_data_point_recency
                else "—"
            )
            recalculated = (
                skin.last_price_recalculated_at.strftime("%Y-%m-%d %H:%M")
                if skin.last_price_recalculated_at
                else "—"
            )
            table.add_row(
                skin.name, skin.collection_name or "—", skin.rarity_name or "—", variant,
                last_price, recency, recalculated, key=skin.id,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._reload_skins(event.value)
