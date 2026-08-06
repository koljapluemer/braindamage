"""Mono trades: the EV of the laziest possible trade-up.

For every (collection, rarity tier, StatTrak/not) that can legally be traded up,
find the single cheapest priced input skin+wear and price out a contract built
from 10x that one item. This is a batch survey, not an interactive builder — it
reuses tradeup.py's contract/simulation machinery directly so the numbers here
always agree with the interactive simulator by construction, rather than by a
parallel implementation staying in sync.

A prior version of this module (pre "move to terminal CLI") queried a batch
per-collection price table (MarketItem) that no longer exists — pricing is now
resolved per skin, per wear, from JSON signal files (see braindamage.pricing),
so finding the cheapest input here means scanning each eligible skin's five
wear buckets individually rather than one batched collection-wide query.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

from sqlalchemy.orm import Session

from . import contracts as contracts_module
from . import pricing, tradeup
from .models import Contract, Skin

DEFAULT_TOP_N = 25


def _representative_float(skin: Skin, wear_name: str) -> float:
    """A single float standing in for 'a copy of this skin at this wear' — there's
    no real listing to read a float from, so this picks the midpoint of the wear
    bucket clipped to the skin's own float range (falling back to the skin's own
    midpoint if the skin's range doesn't reach that bucket at all)."""
    min_f = skin.min_float if skin.min_float is not None else 0.0
    max_f = skin.max_float if skin.max_float is not None else 1.0
    lo, hi = tradeup.wear_bucket_range(wear_name)
    lo, hi = max(lo, min_f), min(hi, max_f)
    if lo > hi:
        return (min_f + max_f) / 2
    return (lo + hi) / 2


def _cheapest_input(skins: list[Skin]) -> tuple[Skin, str] | None:
    """The (skin, wear_name) pair with the lowest known price among `skins` —
    all from one collection/rarity/StatTrak group already. Checks every skin
    against every wear bucket since there's no batched price-by-collection
    query available against the current schema."""
    best: tuple[Skin, str, float] | None = None
    for skin in skins:
        for wear_name, _lo, _hi in tradeup.WEAR_BUCKETS:
            price_info = pricing.latest_price_for_wear(skin.id, wear_name)
            if price_info is None:
                continue
            price, _observed_at = price_info
            if best is None or price < best[2]:
                best = (skin, wear_name, price)
    return None if best is None else (best[0], best[1])


def generate_mono_trades(
    session: Session,
    *,
    max_input_cost: float,
    top_n: int = DEFAULT_TOP_N,
    on_progress: Callable[[int, int], None] | None = None,
    on_collection_progress: Callable[[int, int], None] | None = None,
    on_upsert_progress: Callable[[int, int], None] | None = None,
) -> list[Contract]:
    """Every collection x tier x StatTrak mono trade (10x the single cheapest
    priced input for that combo) that costs at most `max_input_cost`, ranked by
    net expected value (highest first), upserted as Contract rows and capped to
    `top_n`. Returns the upserted rows.

    `on_progress`, if given, is called as `on_progress(done, total)` once per
    (rarity, StatTrak) combo finished — deliberately generic rather than a
    UI-specific callback, so this module stays UI-framework-agnostic. That's
    coarse (10 combos total): the actual work happens per-collection inside a
    combo (`_cheapest_input` reads every candidate skin's price signals off
    disk) and per-upsert (each kept candidate re-simulates a dense 1001-sample
    EV curve) -- `on_collection_progress`/`on_upsert_progress`, if given, report
    those finer-grained totals instead, for callers that want feedback during
    what's actually the slow part.
    """
    combos = list(itertools.product(tradeup.INPUT_RARITIES, (False, True)))
    total_combos = len(combos)
    candidates: list[tuple[tradeup.ContractState, tradeup.SimulationResult]] = []

    # Collection membership is a cheap DB-only lookup -- resolved for every combo
    # up front so the total collection count (the real progress denominator) is
    # known before the expensive per-collection price scanning starts.
    combo_collections: list[tuple[str, bool, dict[str, list[Skin]]]] = []
    for rarity_name, stattrak in combos:
        skins_by_collection: dict[str, list[Skin]] = {}
        for skin in tradeup.eligible_input_skins(session, rarity_name, stattrak):
            skins_by_collection.setdefault(skin.collection_id, []).append(skin)
        combo_collections.append((rarity_name, stattrak, skins_by_collection))

    total_collections = sum(len(skins_by_collection) for _, _, skins_by_collection in combo_collections)
    collections_done = 0

    for done, (rarity_name, stattrak, skins_by_collection) in enumerate(combo_collections, start=1):
        for collection_skins in skins_by_collection.values():
            cheapest = _cheapest_input(collection_skins)
            collections_done += 1
            if on_collection_progress is not None:
                on_collection_progress(collections_done, total_collections)
            if cheapest is None:
                continue
            skin, wear_name = cheapest

            contract = tradeup.ContractState(
                rarity_name=rarity_name,
                stattrak=stattrak,
                lines=[
                    tradeup.ContractLine(
                        skin_id=skin.id,
                        skin_name=skin.name,
                        collection_id=skin.collection_id,
                        collection_name=skin.collection_name,
                        float_value=_representative_float(skin, wear_name),
                        quantity=10,
                    )
                ],
            )
            result = tradeup.simulate_contract(session, contract)
            if 0 < result.input_cost <= max_input_cost:
                candidates.append((contract, result))

        if on_progress is not None:
            on_progress(done, total_combos)

    candidates.sort(key=lambda c: c[1].expected_value, reverse=True)
    selected = candidates[:top_n]
    total_upserts = len(selected)
    results = []
    for done, (contract, result) in enumerate(selected, start=1):
        results.append(contracts_module.upsert_contract(session, contract, result))
        if on_upsert_progress is not None:
            on_upsert_progress(done, total_upserts)
    return results
