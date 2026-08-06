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
    simulate_ev_curve,
)


def _optimization_ranges(points: list[dict], limit: int = 3) -> list[dict]:
    """Collapse adjacent, economically identical curve samples into ranges.

    Prices change at wear boundaries, so EV is piecewise constant.  Grouping
    those plateaus exposes the useful buying tolerances instead of presenting
    individual samples as if their precision were meaningful.
    """
    if not points:
        return []
    groups: list[list[dict]] = []
    for point in points:
        signature = (round(point["input_cost"], 8), round(point["expected_revenue"], 8), round(point["stdev"], 8))
        previous = groups[-1][-1] if groups else None
        previous_signature = None if previous is None else (
            round(previous["input_cost"], 8), round(previous["expected_revenue"], 8), round(previous["stdev"], 8)
        )
        if signature != previous_signature:
            groups.append([])
        groups[-1].append(point)

    ranges = []
    for group in groups:
        representative = group[0]
        revenue = representative["expected_revenue"]
        cost = representative["input_cost"]
        ev = representative["expected_value"]
        ranges.append({
            "min_float": group[0]["raw_avg_float"],
            "max_float": group[-1]["raw_avg_float"],
            "min_normalized_float": group[0]["avg_float"],
            "max_normalized_float": group[-1]["avg_float"],
            "expected_price": revenue,
            "expected_value": ev,
            "roi": ev / cost if cost > 0 else None,
            "cvar_5pct": representative["cvar_5pct"],
            "outcome": "Guaranteed profit" if representative["worst_profit"] >= 0 else "Positive EV" if ev >= 0 else "Negative EV",
        })
    return sorted(ranges, key=lambda r: (r["expected_value"], r["roi"] or float("-inf")), reverse=True)[:limit]


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

    # Computed before `row` is touched: simulate_ev_curve issues its own
    # queries, and running it after row.add()/attribute assignment risks an
    # autoflush mid-mutation that tries (and NOT-NULL-fails) to insert `row`
    # before every column is set.
    curve_samples = simulate_ev_curve(session, contract)
    ev_curve_points = [
        {
            "avg_float": p.avg_float,
            "input_cost": p.input_cost,
            "expected_revenue": p.expected_revenue,
            "expected_value": p.expected_value,
            "stdev": p.stdev,
        }
        for p in curve_samples
    ]
    ev_curve_annotations = [
        {"raw_avg_float": p.raw_avg_float, "worst_profit": p.worst_profit}
        for p in curve_samples
    ]
    dense_points = [
        {
            "avg_float": p.avg_float, "raw_avg_float": p.raw_avg_float,
            "input_cost": p.input_cost, "expected_revenue": p.expected_revenue,
            "expected_value": p.expected_value, "stdev": p.stdev,
            "worst_profit": p.worst_profit,
            "cvar_5pct": p.cvar_5pct,
        }
        for p in simulate_ev_curve(session, contract, n_samples=1001)
    ]
    optimization_ranges = _optimization_ranges(dense_points)

    row_id = contract_id(contract)
    now = now_utc()
    row = session.get(Contract, row_id)
    if row is None:
        row = Contract(id=row_id, created_at=now, favorite=False)
        session.add(row)

    row.rarity_name = contract.rarity_name
    row.target_rarity_name = target_rarity
    row.stattrak = contract.stattrak
    complete = not result.missing_input_price_names and not result.missing_output_price_names
    best = optimization_ranges[0] if optimization_ranges and complete else None
    row.input_cost = result.input_cost if best is None else best["expected_price"] - best["expected_value"]
    row.expected_output_value = result.expected_output_value if best is None else best["expected_price"]
    row.expected_value = result.expected_value if best is None else best["expected_value"]
    row.roi = result.roi if best is None else best["roi"]
    row.cvar_5pct = cvar(outcome_profits(result), alpha=0.05) if best is None else best["cvar_5pct"]
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
    row.ev_curve = ev_curve_points
    row.ev_curve_annotations = ev_curve_annotations
    row.optimization_ranges = optimization_ranges

    session.commit()
    return row


def is_calculable(contract: Contract) -> bool:
    """False if any input/output price was missing at simulation time, meaning
    `contract`'s EV/ROI/CVaR were computed against incomplete data."""
    return not contract.missing_input_price_names and not contract.missing_output_price_names


def filter_contracts(
    contracts: list[Contract],
    *,
    hide_bad_trades: bool = True,
    hide_uncalculable_trades: bool = True,
    max_cost: float = 0.0,
) -> list[Contract]:
    """Contracts list page filter: drop negative-EV contracts, drop contracts
    simulated with missing prices, and cap input_cost -- `max_cost <= 0` means
    no cap."""
    result = contracts
    if hide_bad_trades:
        result = [c for c in result if c.expected_value >= 0]
    if hide_uncalculable_trades:
        result = [c for c in result if is_calculable(c)]
    if max_cost > 0:
        result = [c for c in result if c.input_cost <= max_cost]
    return result


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
