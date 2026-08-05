"""Controller owning the in-progress contract's state.

Direct port of the plain attributes ContractBuilderScreen held on itself
(lines/rarity_name/stattrak/last_result/last_contract_id/last_favorite), moved
into its own QObject so the page widget has exactly one `stateChanged` handler
that re-renders everything after any mutation -- the same "mutate then
explicitly re-render, single owner of state" discipline the Textual version
used deliberately to avoid Streamlit-style state bugs (see root CLAUDE.md).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ... import contracts as contracts_module
from ... import tradeup
from ...db import SessionLocal
from ...models import Skin
from ...tradeup import ContractLine, SimulationResult, SkinOption


class ContractBuilderController(QObject):
    stateChanged = Signal()
    statusChanged = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.lines: list[ContractLine] = []
        self.rarity_name: str | None = None
        self.stattrak: bool | None = None
        self.last_result: SimulationResult | None = None
        self.last_contract_id: str | None = None
        self.last_favorite: bool = False

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    def eligible_options(self) -> list[SkinOption]:
        with SessionLocal() as session:
            options = tradeup.eligible_input_options(session)
        if self.rarity_name is not None:
            options = [o for o in options if o.rarity_name == self.rarity_name and o.stattrak == self.stattrak]
        return options

    def option_for_existing_line(self, index: int) -> SkinOption | None:
        line = self.lines[index]
        with SessionLocal() as session:
            skin = session.get(Skin, line.skin_id)
        if skin is None:
            self.statusChanged.emit(f"{line.skin_name} no longer exists in the catalog.")
            return None
        return SkinOption(
            skin_id=skin.id,
            skin_name=skin.name,
            collection_id=skin.collection_id,
            collection_name=skin.collection_name,
            rarity_name=skin.rarity_name,
            stattrak=skin.stattrak,
            min_float=skin.min_float if skin.min_float is not None else 0.0,
            max_float=skin.max_float if skin.max_float is not None else 1.0,
        )

    def add_line(self, option: SkinOption, float_value: float, quantity: int) -> None:
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
        self.statusChanged.emit(f"Added {option.skin_name}.")
        self.stateChanged.emit()

    def edit_line(self, index: int, float_value: float, quantity: int) -> None:
        self.lines[index].float_value = float_value
        self.lines[index].quantity = quantity
        self.stateChanged.emit()

    def delete_line(self, index: int) -> None:
        removed = self.lines.pop(index)
        if not self.lines:
            self.rarity_name = None
            self.stattrak = None
        self.statusChanged.emit(f"Removed {removed.skin_name}.")
        self.stateChanged.emit()

    def new_contract(self) -> None:
        self.lines = []
        self.rarity_name = None
        self.stattrak = None
        self.last_result = None
        self.last_contract_id = None
        self.last_favorite = False
        self.statusChanged.emit("Started a new contract. Click 'Add input' to add an input.")
        self.stateChanged.emit()

    def run_simulation(self) -> None:
        if self.total_quantity != 10 or self.rarity_name is None:
            self.statusChanged.emit(f"Need exactly 10 inputs to simulate (currently {self.total_quantity}/10).")
            return
        contract = tradeup.ContractState(
            rarity_name=self.rarity_name, stattrak=bool(self.stattrak), lines=list(self.lines)
        )
        with SessionLocal() as session:
            result = tradeup.simulate_contract(session, contract)
            row = contracts_module.upsert_contract(session, contract, result)
            self.last_contract_id = row.id
            self.last_favorite = row.favorite
        self.last_result = result
        self.statusChanged.emit("Simulation complete — click 'Toggle favorite' to favorite this contract.")
        self.stateChanged.emit()

    def toggle_favorite(self) -> None:
        if self.last_contract_id is None:
            self.statusChanged.emit("Run a simulation before favoriting it.")
            return
        with SessionLocal() as session:
            contracts_module.set_favorite(session, self.last_contract_id, not self.last_favorite)
        self.last_favorite = not self.last_favorite
        self.stateChanged.emit()
