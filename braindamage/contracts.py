"""Persisting simulated trade-up contracts (see braindamage.tradeup) as Contract
rows, keyed by the exact input composition so re-running the same combination
upserts instead of duplicating.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from .models import Contract
from .signals import now_utc
from .tradeup import (
    ContractLine,
    ContractState,
    SimulationResult,
    cvar,
    next_rarity,
    outcome_profits,
    simulate_contract,
)


def contract_id(contract: ContractState) -> str:
    """Deterministic id for a contract's exact composition: same rarity/StatTrak
    and the same (skin, float, quantity) lines, in any order, always hash to the
    same id — so re-simulating an unchanged contract updates its row instead of
    creating a duplicate."""
    parts = sorted((line.skin_id, round(line.float_value, 4), line.quantity) for line in contract.lines)
    payload = repr((contract.rarity_name, contract.stattrak, parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def upsert_contract(session: Session, contract: ContractState, result: SimulationResult) -> Contract:
    """Persists (inserts or updates) the Contract row for `contract`'s exact
    composition, from a simulation `result` already computed by
    tradeup.simulate_contract. Favorite status and creation time survive a
    re-simulation of the same composition."""
    target_rarity = next_rarity(contract.rarity_name)
    if target_rarity is None:
        raise ValueError(f"{contract.rarity_name} has no next rarity tier")

    row_id = contract_id(contract)
    now = now_utc()
    row = session.get(Contract, row_id)
    if row is None:
        row = Contract(id=row_id, created_at=now, favorite=False)
        session.add(row)

    row.rarity_name = contract.rarity_name
    row.target_rarity_name = target_rarity
    row.stattrak = contract.stattrak
    row.input_cost = result.input_cost
    row.expected_output_value = result.expected_output_value
    row.expected_value = result.expected_value
    row.roi = result.roi
    row.cvar_5pct = cvar(outcome_profits(result), alpha=0.05)
    row.last_simulated_at = now
    row.input_lines = [
        {
            "skin_id": line.skin_id,
            "skin_name": line.skin_name,
            "collection_id": line.collection_id,
            "collection_name": line.collection_name,
            "float_value": line.float_value,
            "quantity": line.quantity,
        }
        for line in contract.lines
    ]
    row.outcomes = [
        {
            "skin_id": o.skin_id,
            "skin_name": o.skin_name,
            "collection_name": o.collection_name,
            "probability": o.probability,
            "predicted_float": o.predicted_float,
            "predicted_wear": o.predicted_wear,
            "gross_price": o.gross_price,
            "net_price": o.net_price,
            "contribution": o.contribution,
        }
        for o in result.outcomes
    ]
    row.missing_input_price_names = result.missing_input_price_names
    row.missing_output_price_names = result.missing_output_price_names

    session.commit()
    return row


def set_favorite(session: Session, contract_row_id: str, favorite: bool) -> None:
    row = session.get(Contract, contract_row_id)
    if row is None:
        return
    row.favorite = favorite
    session.commit()


def state_from_row(contract: Contract) -> ContractState:
    """Rebuilds the ContractState a persisted `contract` was simulated from --
    input_lines already stores exactly ContractLine's fields, by construction
    of upsert_contract above."""
    return ContractState(
        rarity_name=contract.rarity_name,
        stattrak=contract.stattrak,
        lines=[ContractLine(**line) for line in contract.input_lines],
    )


def referenced_skin_ids(contract: Contract) -> set[str]:
    """Every skin id `contract` touches, as an input line or a possible
    output -- what a price-refresh action (Steam, CS2Cap, ...) needs to fetch
    for one contract."""
    return {line["skin_id"] for line in contract.input_lines} | {o["skin_id"] for o in contract.outcomes}


def resimulate(session: Session, contract: Contract) -> Contract:
    """Re-runs `contract`'s simulation from whatever prices are currently on
    disk and upserts the refreshed row -- the shared last step after any
    price-refresh action has updated this contract's skins' signal files."""
    state = state_from_row(contract)
    result = simulate_contract(session, state)
    return upsert_contract(session, state, result)
