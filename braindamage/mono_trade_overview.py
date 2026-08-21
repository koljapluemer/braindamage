"""Compact data model for the browser extension's mono-trade overview."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from . import mono_trade_table, signals, tradeup
from .models import Skin


def _latest_offer_prices_by_wear(skin_id: str, input_source: str) -> dict[str, float]:
    """Cheapest last-known listing per wear from `input_source` ("steam" or
    "csfloat", see mono_trade_table.INPUT_SOURCES), with repeated sightings
    deduped. CSFloat listings (MarketOfferSignal) carry a real listing_id, so
    that alone is the dedup key, and are restricted to listing_type ==
    "buy_now" -- an auction's displayed price is only its current bid, not a
    price actually payable right now, same exclusion as mono_trade_table's
    own CSFloat offer gathering (see its _offers_for_wear docstring). Steam
    listings (SteamOfferSignal, which exposes no stable ID) fall back to
    (float_value, pattern_seed, price), same as mono_trade_table's own
    dedup."""
    by_wear: dict[str, dict] = defaultdict(dict)
    if input_source == "csfloat":
        offers = [
            o for o in signals.read_market_offers(skin_id) if o.source == "csfloat" and o.listing_type == "buy_now"
        ]
        key = lambda o: o.listing_id
    else:
        offers = signals.read_steam_offers(skin_id)
        key = lambda o: (o.float_value, o.pattern_seed, o.price)
    for offer in offers:
        if offer.wear_name:
            current = by_wear[offer.wear_name].get(key(offer))
            if current is None or offer.fetched_at > current.fetched_at:
                by_wear[offer.wear_name][key(offer)] = offer

    return {wear: min(offer.price for offer in offers.values()) for wear, offers in by_wear.items()}


def _best_known_input_price(
    skin: Skin, legacy_prices: dict[str, tuple[float, datetime]], input_source: str
) -> float | None:
    """Lowest wear price, preferring scraped offers over legacy price signals."""
    offer_prices = _latest_offer_prices_by_wear(skin.id, input_source)
    candidates = []
    for wear, _lo, _hi in tradeup.WEAR_BUCKETS:
        if wear in offer_prices:
            candidates.append(offer_prices[wear])
            continue
        legacy = legacy_prices.get(wear)
        if legacy is not None:
            candidates.append(legacy[0])
    return min(candidates) if candidates else None


def _price_band(value: float) -> int | None:
    return math.floor(math.log10(value)) if value > 0 else None


def _best_naive_ev(
    session: Session,
    skins: list[Skin],
    legacy_prices_by_skin: dict[str, dict[str, tuple[float, datetime]]],
    input_source: str,
) -> float | None:
    values = []
    for skin in skins:
        table = mono_trade_table.build_table(
            session, skin, legacy_prices_by_skin=legacy_prices_by_skin, input_source=input_source
        )
        values.extend(
            row["ev_cell"]["value"]
            for row in table["rows"]
            if row["ev_cell"]["value"] is not None
        )
    return max(values) if values else None


def build_overview(
    session: Session,
    *,
    rarities: list[str] | None = None,
    stattrak_values: list[bool] | None = None,
    input_source: str = mono_trade_table.DEFAULT_INPUT_SOURCE,
) -> dict:
    """Return every valid collection/rarity/variant mono-trade as JSON data,
    with input prices drawn from `input_source` ("steam" or "csfloat", see
    mono_trade_table.INPUT_SOURCES) -- the sidebar's market dropdown."""
    legacy_prices_by_skin: dict[str, dict[str, tuple[float, datetime]]] = {}

    def legacy_prices(skin_id: str) -> dict[str, tuple[float, datetime]]:
        if skin_id not in legacy_prices_by_skin:
            snapshot = signals.read_legacy_price_snapshot(skin_id)
            legacy_prices_by_skin[skin_id] = {
                wear: (entry.price, entry.observed_at)
                for wear, entry in (snapshot.prices_by_wear.items() if snapshot else [])
            }
        return legacy_prices_by_skin[skin_id]

    groups: dict[tuple[str, str, bool], list[Skin]] = defaultdict(list)
    for rarity in rarities if rarities is not None else tradeup.INPUT_RARITIES:
        for stattrak in stattrak_values if stattrak_values is not None else (False, True):
            for skin in tradeup.eligible_input_skins(session, rarity, stattrak):
                groups[(skin.collection_id, rarity, stattrak)].append(skin)

    rows = []
    for (_collection_id, rarity, stattrak), skins in groups.items():
        skins.sort(key=lambda skin: skin.name)
        histories = [entry for skin in skins for entry in signals.read_contract_history(skin.id)]
        persisted = bool(histories)
        ev = max((entry.expected_value for entry in histories), default=None)
        if ev is None:
            # Prime output snapshots too: build_table receives this mapping and
            # must never fall through to the full historical pricing reader.
            target = tradeup.next_rarity(rarity)
            output_skins = session.query(Skin).filter(
                Skin.collection_id == skins[0].collection_id,
                Skin.rarity_name == target,
                Skin.stattrak.is_(stattrak),
                Skin.souvenir.is_(False),
            ).all()
            for output in output_skins:
                legacy_prices(output.id)
            ev = _best_naive_ev(session, skins, legacy_prices_by_skin, input_source)

        prices = {
            skin.id: _best_known_input_price(skin, legacy_prices(skin.id), input_source) for skin in skins
        }
        known_prices = [price for price in prices.values() if price is not None]
        cheapest = min(known_prices) if known_prices else None
        cheapest_band = _price_band(cheapest) if cheapest is not None else None

        input_skins = []
        for skin in skins:
            price = prices[skin.id]
            is_cheapest = cheapest is not None and price is not None and math.isclose(price, cheapest)
            input_skins.append(
                {
                    "skin_id": skin.id,
                    "skin_name": skin.name,
                    "steam_url": mono_trade_table._steam_listing_url(skin),
                    "price_emphasis": (
                        "cheapest"
                        if is_cheapest
                        else "same_range"
                        if price is not None and _price_band(price) == cheapest_band
                        else None
                    ),
                }
            )

        rows.append(
            {
                "collection_name": skins[0].collection_name or "Unknown collection",
                "rarity_name": rarity,
                "stattrak": stattrak,
                "expected_value": ev,
                "ev_source": "persisted" if persisted else "naive",
                "input_skins": input_skins,
            }
        )

    rows.sort(key=lambda row: (row["collection_name"], row["rarity_name"], row["stattrak"]))
    return {"ok": True, "trades": rows}
