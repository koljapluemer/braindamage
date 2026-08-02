"""Simple Mono Trades: the EV of the laziest possible trade-up.

For every (collection, rarity tier, StatTrak/not) that can legally be traded up,
find the single cheapest tradeable input skin+wear and price out a contract built
from 10x that one item. This is a batch survey, not an interactive builder — it
reuses tradeup.py's contract/simulation machinery directly so the numbers here
always agree with the interactive simulator by construction, rather than by a
parallel implementation staying in sync.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import tradeup
from .models import FavoriteMonoTrade, MarketItem, Skin
from .pricing import latest_prices


@dataclass
class MonoTradeCandidate:
    collection_id: str
    collection_name: str
    rarity_name: str
    stattrak: bool
    skin_name: str
    wear_name: str | None
    unit_price: float
    result: tradeup.SimulationResult

    @property
    def favorite_key(self) -> tuple[str, str, bool]:
        return (self.collection_id, self.rarity_name, self.stattrak)


def _representative_float(skin: Skin, wear_name: str | None) -> float:
    """A single float standing in for 'a copy of this skin at this wear' — there's
    no real listing to read a float from, so this picks the midpoint of the wear
    bucket clipped to the skin's own float range (falling back to the skin's own
    midpoint if the skin's range doesn't reach that bucket at all)."""
    min_f = skin.min_float if skin.min_float is not None else 0.0
    max_f = skin.max_float if skin.max_float is not None else 1.0
    if wear_name is None:
        return (min_f + max_f) / 2
    lo, hi = tradeup.wear_bucket_range(wear_name)
    lo, hi = max(lo, min_f), min(hi, max_f)
    if lo > hi:
        return (min_f + max_f) / 2
    return (lo + hi) / 2


def _cheapest_inputs_by_collection(
    session: Session, skins: list[Skin], stattrak: bool
) -> dict[str, tuple[Skin, MarketItem, float]]:
    """Cheapest (skin, wear) market item per collection_id, among `skins` — all
    from one rarity+StatTrak tier already. Batched across every skin in one query
    rather than per-collection, since per-collection would repeat the same
    latest_prices work once per collection for no benefit."""
    if not skins:
        return {}

    skins_by_id = {s.id: s for s in skins}
    market_items = list(
        session.scalars(
            select(MarketItem)
            .where(MarketItem.skin_id.in_(skins_by_id))
            .where(MarketItem.stattrak.is_(stattrak))
            .where(MarketItem.souvenir.is_(False))
        ).all()
    )
    prices = latest_prices(session, [mi.id for mi in market_items])

    best_by_collection: dict[str, tuple[Skin, MarketItem, float]] = {}
    for market_item in market_items:
        price = prices.get(market_item.id)
        if price is None:
            continue
        skin = skins_by_id[market_item.skin_id]
        existing = best_by_collection.get(skin.collection_id)
        if existing is None or price < existing[2]:
            best_by_collection[skin.collection_id] = (skin, market_item, price)
    return best_by_collection


def _build_mono_contract(
    skin: Skin, market_item: MarketItem, rarity_name: str, stattrak: bool
) -> tradeup.ContractState:
    """A ready-to-simulate 10x-one-item contract, built the same way the
    interactive page assembles one line at a time."""
    contract = tradeup.ContractState(rarity_name=rarity_name, stattrak=stattrak)
    contract.lines.append(
        tradeup.ContractLine(
            market_item_id=market_item.id,
            skin_id=skin.id,
            skin_name=skin.name,
            collection_id=skin.collection_id,
            collection_name=skin.collection.name,
            wear_name=market_item.wear_name,
            float_value=_representative_float(skin, market_item.wear_name),
            quantity=10,
        )
    )
    return contract


def find_mono_trades(
    session: Session,
    top_n: int | None = 25,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[MonoTradeCandidate]:
    """Every collection x tier x StatTrak mono trade, simulated and ranked by net
    expected value (highest first), capped to `top_n` (or unlimited if None).

    `on_progress`, if given, is called as `on_progress(done, total)` after each of
    the `total` (rarity, StatTrak) tiers finishes — deliberately generic rather
    than a Streamlit-specific callback, so this module has no UI dependency and
    stays testable without one.
    """
    combos = [(rarity_name, stattrak) for rarity_name in tradeup.INPUT_RARITIES for stattrak in (False, True)]
    candidates: list[MonoTradeCandidate] = []

    for done, (rarity_name, stattrak) in enumerate(combos, start=1):
        skins = tradeup.eligible_input_skins(session, rarity_name, stattrak)
        cheapest_by_collection = _cheapest_inputs_by_collection(session, skins, stattrak)

        for skin, market_item, unit_price in cheapest_by_collection.values():
            contract = _build_mono_contract(skin, market_item, rarity_name, stattrak)
            result = tradeup.simulate_contract(session, contract)
            candidates.append(
                MonoTradeCandidate(
                    collection_id=skin.collection_id,
                    collection_name=skin.collection.name,
                    rarity_name=rarity_name,
                    stattrak=stattrak,
                    skin_name=skin.name,
                    wear_name=market_item.wear_name,
                    unit_price=unit_price,
                    result=result,
                )
            )

        if on_progress is not None:
            on_progress(done, len(combos))

    candidates.sort(key=lambda c: c.result.expected_value, reverse=True)
    return candidates if top_n is None else candidates[:top_n]


def mono_trade_candidate_for(
    session: Session, collection_id: str, rarity_name: str, stattrak: bool
) -> MonoTradeCandidate | None:
    """The current cheapest mono-trade contract for one specific (collection,
    rarity, StatTrak) combo — same construction as find_mono_trades, just
    scoped to one combo instead of surveying all of them. Used by the
    favorites page to re-simulate a starred combo against current prices,
    since the cheapest input can shift over time. Returns None if the combo
    no longer has a priced, eligible cheapest input."""
    skins = [
        s for s in tradeup.eligible_input_skins(session, rarity_name, stattrak) if s.collection_id == collection_id
    ]
    cheapest_by_collection = _cheapest_inputs_by_collection(session, skins, stattrak)
    entry = cheapest_by_collection.get(collection_id)
    if entry is None:
        return None

    skin, market_item, unit_price = entry
    contract = _build_mono_contract(skin, market_item, rarity_name, stattrak)
    result = tradeup.simulate_contract(session, contract)
    return MonoTradeCandidate(
        collection_id=collection_id,
        collection_name=skin.collection.name,
        rarity_name=rarity_name,
        stattrak=stattrak,
        skin_name=skin.name,
        wear_name=market_item.wear_name,
        unit_price=unit_price,
        result=result,
    )


# --- Favorites ---------------------------------------------------------------


def favorite_keys(session: Session) -> set[tuple[str, str, bool]]:
    """Every currently-favorited (collection_id, rarity_name, StatTrak) combo."""
    rows = session.execute(
        select(FavoriteMonoTrade.collection_id, FavoriteMonoTrade.rarity_name, FavoriteMonoTrade.stattrak)
    ).all()
    return {(r.collection_id, r.rarity_name, r.stattrak) for r in rows}


def add_favorite(session: Session, collection_id: str, rarity_name: str, stattrak: bool) -> None:
    exists = session.execute(
        select(FavoriteMonoTrade.id)
        .where(FavoriteMonoTrade.collection_id == collection_id)
        .where(FavoriteMonoTrade.rarity_name == rarity_name)
        .where(FavoriteMonoTrade.stattrak.is_(stattrak))
    ).first()
    if exists is None:
        session.add(FavoriteMonoTrade(collection_id=collection_id, rarity_name=rarity_name, stattrak=stattrak))
        session.commit()


def remove_favorite(session: Session, collection_id: str, rarity_name: str, stattrak: bool) -> None:
    session.execute(
        delete(FavoriteMonoTrade)
        .where(FavoriteMonoTrade.collection_id == collection_id)
        .where(FavoriteMonoTrade.rarity_name == rarity_name)
        .where(FavoriteMonoTrade.stattrak.is_(stattrak))
    )
    session.commit()
