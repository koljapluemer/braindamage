"""Builds the mono-trade-up price table shown in the browser extension's
sidebar (webext/) for whichever skin the user currently has a Steam
Community Market listing page open for -- one row per wear tier, one column
per possible trade-up outcome skin at that collection/rarity, using whatever
price/offer signals are already on disk (see braindamage/signals.py).
Read-only: makes no network calls and writes nothing itself --
braindamage.steam_offers_host calls this right after it writes whatever the
sidebar just scraped, so the table reflects that scrape immediately.

Deliberately simpler than braindamage.tradeup's real simulator: every row
prices its 10 inputs and every outcome at exactly that one wear bucket,
ignoring the possibility of mixed input wears or an outcome float drifting
into a neighboring wear bucket -- see spec.md for why that simplification is
acceptable for this "at a glance" sidebar view.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pricing, signals
from .market_names import market_hash_name
from .models import Skin
from .signals import now_utc
from .tradeup import SELL_FEE_RATE, WEAR_BUCKETS, next_rarity, wear_for_float

REQUIRED_INPUTS = 10
STEAM_LISTING_BASE_URL = "https://steamcommunity.com/market/listings/730/"

_NON_WEAPON_CATEGORIES = ["Knives", "Gloves"]

# Age thresholds for the input-cost cell's background color (see
# _cheapest_ten_cost/_age_color): purple = still fresh enough to trust at a
# glance, green = today's data, orange = anything older. Outcome cells use
# the same thresholds when priced from a buy-order summary; a fallback
# (non-buy-order) outcome price is always grey instead, regardless of age --
# see _outcome_price_cell.
_FRESH_AGE = timedelta(hours=1)
_RECENT_AGE = timedelta(days=1)


class MonoTradeTableError(RuntimeError):
    """`skin` isn't usable as a mono trade-up input (or its collection has no
    eligible output at the next rarity tier) -- no table can be built for it."""


def _steam_listing_url(skin: Skin) -> str:
    """A working Steam Community Market listing URL for `skin` -- any valid
    wear works, since that page lists every wear condition of one weapon
    together, so the midpoint of the skin's own float range is used just to
    pick one wear name that's guaranteed to be within range."""
    min_f = skin.min_float if skin.min_float is not None else 0.0
    max_f = skin.max_float if skin.max_float is not None else 1.0
    wear = wear_for_float((min_f + max_f) / 2)
    name = market_hash_name(skin, wear)
    return STEAM_LISTING_BASE_URL + urllib.parse.quote(name, safe="")


def _age_color(fetched_at: datetime) -> str:
    age = now_utc() - fetched_at
    if age <= _FRESH_AGE:
        return "purple"
    if age <= _RECENT_AGE:
        return "green"
    return "orange"


def _cheapest_ten_cost(skin_id: str, wear_name: str) -> tuple[float, datetime] | None:
    """Sum of the 10 cheapest Steam offers on disk for `skin_id` at
    `wear_name`, plus the oldest fetch time among those 10 (the cell's
    color is keyed off the *oldest* data point used, not the newest, so the
    color reflects the staleness of the whole calculation) -- None if fewer
    than REQUIRED_INPUTS offers are on disk for that wear.

    Offers are deduped by (float_value, pattern_seed, price), the same
    synthetic identity braindamage.steam_offer_combos uses, keeping each
    key's most-recently-fetched snapshot. Unlike that module, this applies no
    freshness cutoff: the point of this table is "what's on disk right now,
    however stale", colored by age rather than silently dropped.
    """
    latest_by_key: dict[tuple[float | None, int | None, float], signals.SteamOfferSignal] = {}
    for offer in signals.read_steam_offers(skin_id):
        if offer.wear_name != wear_name:
            continue
        key = (offer.float_value, offer.pattern_seed, offer.price)
        existing = latest_by_key.get(key)
        if existing is None or offer.fetched_at > existing.fetched_at:
            latest_by_key[key] = offer

    offers = sorted(latest_by_key.values(), key=lambda o: o.price)
    if len(offers) < REQUIRED_INPUTS:
        return None
    cheapest = offers[:REQUIRED_INPUTS]
    return sum(o.price for o in cheapest), min(o.fetched_at for o in cheapest)


def _outcome_price_cell(skin_id: str, wear_name: str) -> dict:
    """The best available sell-side price for one outcome skin at one wear:
    a buy-order-book summary if one's on disk (the best possible outcome
    price -- an instant sale, no listing needed), colored by its own age;
    otherwise whatever braindamage.pricing's general last-price resolution
    has, colored grey regardless of age since it's a materially weaker
    signal (see spec.md)."""
    buy_order = pricing.latest_buy_order_for_wear(skin_id, wear_name)
    if buy_order is not None:
        price, fetched_at, _num_orders = buy_order
        return {"value": price, "color": _age_color(fetched_at), "source": "buy_order"}

    price_info = pricing.latest_price_for_wear(skin_id, wear_name)
    if price_info is None:
        return {"value": None, "color": None, "source": None}
    price, _observed_at = price_info
    return {"value": price, "color": "grey", "source": "fallback"}


def build_table(session: Session, skin: Skin) -> dict:
    """The full sidebar table for `skin`: one row per wear tier, with `skin`'s
    own buy-10-cost, every possible mono-trade outcome's price, and a
    per-row EV. Raises MonoTradeTableError if `skin` isn't a valid trade-up
    input at all, or its collection has no eligible output to trade into."""
    if skin.category_name in _NON_WEAPON_CATEGORIES or skin.souvenir or skin.collection_id is None:
        raise MonoTradeTableError(f"{skin.name} isn't a usable trade-up input.")
    target_rarity = next_rarity(skin.rarity_name) if skin.rarity_name else None
    if target_rarity is None:
        raise MonoTradeTableError(
            f"{skin.name} ({skin.rarity_name}) has no next rarity tier to trade up into."
        )

    output_query = (
        select(Skin)
        .where(Skin.collection_id == skin.collection_id)
        .where(Skin.rarity_name == target_rarity)
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .where(Skin.stattrak.is_(skin.stattrak))
        .where(Skin.souvenir.is_(False))
        .order_by(Skin.name)
    )
    outcome_skins = list(session.scalars(output_query).all())
    if not outcome_skins:
        raise MonoTradeTableError(
            f"{skin.collection_name} has no eligible output at {target_rarity!r}."
        )

    probability = 1.0 / len(outcome_skins)

    outcome_headers = [
        {"skin_id": o.id, "skin_name": o.name, "steam_url": _steam_listing_url(o)} for o in outcome_skins
    ]

    rows = []
    for wear_name, _lo, _hi in WEAR_BUCKETS:
        cost = _cheapest_ten_cost(skin.id, wear_name)
        if cost is None:
            input_cell = {"value": None, "color": None}
        else:
            total, oldest = cost
            input_cell = {"value": total, "color": _age_color(oldest)}

        outcome_cells = [_outcome_price_cell(o.id, wear_name) for o in outcome_skins]

        if input_cell["value"] is None:
            ev_cell = {"value": None}
        else:
            expected_revenue = sum(
                probability * cell["value"] * (1 - SELL_FEE_RATE)
                for cell in outcome_cells
                if cell["value"] is not None
            )
            ev_cell = {"value": expected_revenue - input_cell["value"]}

        rows.append(
            {
                "wear_name": wear_name,
                "input_cell": input_cell,
                "outcome_cells": outcome_cells,
                "ev_cell": ev_cell,
            }
        )

    return {
        "input_header": {"skin_id": skin.id, "skin_name": skin.name, "steam_url": _steam_listing_url(skin)},
        "outcome_headers": outcome_headers,
        "rows": rows,
    }
