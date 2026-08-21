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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pricing, signals
from .market_names import market_hash_name
from .models import Skin
from .signals import now_utc
from .tradeup import WEAR_BUCKETS, next_rarity, wear_for_float

REQUIRED_INPUTS = 10
STEAM_LISTING_BASE_URL = "https://steamcommunity.com/market/listings/730/"

# Which on-disk offer signal an "input price" comes from -- "steam"
# (SteamOfferSignal, scraped from a Steam Community Market listing page) or
# "csfloat" (MarketOfferSignal with source == "csfloat", scraped from a
# CSFloat search page -- see webext/sidebar.js's CSFloat scraper). Selected by
# the sidebar's market dropdown and threaded through build_table/
# build_float_diagram_data (and braindamage.mono_trade_overview.build_overview,
# which shares this constant) so every input-price consumer stays in sync on
# what "the other" valid value even is.
INPUT_SOURCES = ("steam", "csfloat")
DEFAULT_INPUT_SOURCE = "steam"

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


def _age_color(fetched_at: datetime, *, stale: str = "orange") -> str:
    """purple = fresh, green = recent, `stale` (default "orange", matching
    the sidebar table's cell coloring) otherwise -- `stale` is overridable
    since not every consumer of this age scheme uses the same palette (e.g.
    build_float_diagram_data's offer-point dots use "red" instead)."""
    age = now_utc() - fetched_at
    if age <= _FRESH_AGE:
        return "purple"
    if age <= _RECENT_AGE:
        return "green"
    return stale


@dataclass(frozen=True)
class _NormOffer:
    """One input-price offer, stripped down to exactly what _cheapest_ten_cost
    needs, regardless of which on-disk signal type it came from -- see
    _offers_for_wear. `key` is the price-inclusive dedup identity (two
    observations of the same physical listing at the same price collapse to
    one); `identity` is the price-*exclusive* one (the same physical listing
    re-observed at a changed price is still "the same listing" for topup
    purposes -- see _cheapest_ten_cost)."""

    price: float
    fetched_at: datetime
    key: Any
    identity: Any


def _offers_for_wear(skin_id: str, wear_name: str, source: str) -> list[_NormOffer]:
    """Every on-disk input-price offer for `skin_id` at `wear_name` from
    `source` ("steam" or "csfloat"), normalized to _NormOffer.

    CSFloat listings (signals.MarketOfferSignal) carry a real listing_id, so
    that alone is both the dedup key and the physical-listing identity --
    and are filtered to listing_type == "buy_now" only: an auction's
    displayed price is just the current bid, not a price actually payable
    right now (it can still rise before the auction ends), so it's excluded
    from cost math the same way braindamage.mono_offer_combos already
    excludes auctions from combo construction. Steam listings
    (signals.SteamOfferSignal) expose no stable ID -- see that class's own
    docstring -- so (float_value, pattern_seed, price) stands in as the
    dedup key and (float_value, pattern_seed) as the price-exclusive
    identity, unchanged from this module's pre-CSFloat behavior.
    """
    if source == "csfloat":
        return [
            _NormOffer(price=o.price, fetched_at=o.fetched_at, key=o.listing_id, identity=o.listing_id)
            for o in signals.read_market_offers(skin_id)
            if o.source == "csfloat" and o.wear_name == wear_name and o.listing_type == "buy_now"
        ]
    return [
        _NormOffer(
            price=o.price,
            fetched_at=o.fetched_at,
            key=(o.float_value, o.pattern_seed, o.price),
            identity=(o.float_value, o.pattern_seed) if o.float_value is not None else (o.float_value, o.pattern_seed, o.price),
        )
        for o in signals.read_steam_offers(skin_id)
        if o.wear_name == wear_name
    ]


def _cheapest_ten_cost(skin_id: str, wear_name: str, source: str = DEFAULT_INPUT_SOURCE) -> tuple[float, datetime] | None:
    """Cost to buy REQUIRED_INPUTS offers of `skin_id` at `wear_name` right
    now from `source`, plus the oldest fetch time among the offers actually
    used (the cell's color is keyed off that oldest point, not the newest, so
    the color reflects the staleness of the whole calculation) -- None if
    there still aren't enough offers on disk for that wear/source even after
    the fallback below.

    Prices from the *latest scrape batch* (offers sharing the same
    fetched_at -- one page refresh, see steam_offers_host) whenever that
    batch alone has REQUIRED_INPUTS offers, so the value and its color
    reflect exactly what the last refresh showed, undistorted by however
    cheap some long-gone listing used to be. Only when the latest batch falls
    short does it top up the remaining slots with the cheapest older offers
    on disk (excluding any listing already represented in the batch), same
    as this table always has -- see spec.md.
    """
    offers = _offers_for_wear(skin_id, wear_name, source)
    if not offers:
        return None

    latest_fetched_at = max(o.fetched_at for o in offers)
    batch = [o for o in offers if o.fetched_at == latest_fetched_at]
    older = [o for o in offers if o.fetched_at != latest_fetched_at]

    batch_by_key: dict[Any, _NormOffer] = {}
    for offer in batch:
        batch_by_key[offer.key] = offer
    batch_offers = sorted(batch_by_key.values(), key=lambda o: o.price)

    if len(batch_offers) >= REQUIRED_INPUTS:
        cheapest = batch_offers[:REQUIRED_INPUTS]
        return sum(o.price for o in cheapest), latest_fetched_at

    batch_identities = {o.identity for o in batch_offers}
    older_by_key: dict[Any, _NormOffer] = {}
    for offer in older:
        if offer.identity in batch_identities:
            continue
        existing = older_by_key.get(offer.key)
        if existing is None or offer.fetched_at > existing.fetched_at:
            older_by_key[offer.key] = offer

    shortfall = REQUIRED_INPUTS - len(batch_offers)
    topped_up = sorted(older_by_key.values(), key=lambda o: o.price)[:shortfall]
    selected = batch_offers + topped_up
    if len(selected) < REQUIRED_INPUTS:
        return None
    return sum(o.price for o in selected), min(o.fetched_at for o in selected)


def _outcome_price_cell(
    skin_id: str,
    wear_name: str,
    legacy_prices: dict[str, tuple[float, datetime]] | None = None,
) -> dict:
    """The cell rendering of pricing.net_sell_price_for_wear (see there for
    the actual price resolution, shared with offer_combos' Construct
    Contract search and this module's own build_float_diagram_data so all
    three agree) -- a buy-order price is colored by its own age, a fallback
    price is always grey regardless of age since it's a materially weaker
    signal."""
    resolved = pricing.net_sell_price_for_wear(skin_id, wear_name, legacy_prices=legacy_prices)
    if resolved is None:
        return {"value": None, "color": None, "source": None}
    net_price, observed_at, source = resolved
    color = _age_color(observed_at) if source == "buy_order" else "grey"
    return {"value": net_price, "color": color, "source": source}


def _resolve_outcome_skins(session: Session, skin: Skin) -> list[Skin]:
    """Validates `skin` as a mono trade-up input and returns its eligible
    outcome skins at the next rarity tier (same collection, same
    StatTrak state) -- shared by build_table and build_float_diagram_data,
    which both need exactly this set. Raises MonoTradeTableError if `skin`
    isn't usable as a trade-up input at all, or its collection has no
    eligible output to trade into."""
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
    return outcome_skins


def build_table(
    session: Session,
    skin: Skin,
    *,
    legacy_prices_by_skin: dict[str, dict[str, tuple[float, datetime]]] | None = None,
    input_source: str = DEFAULT_INPUT_SOURCE,
) -> dict:
    """The full sidebar table for `skin`: one row per wear tier, with `skin`'s
    own buy-10-cost (priced from `input_source` -- "steam" or "csfloat", see
    INPUT_SOURCES), every possible mono-trade outcome's price, and a per-row
    EV. Raises MonoTradeTableError if `skin` isn't a valid trade-up input at
    all, or its collection has no eligible output to trade into."""
    outcome_skins = _resolve_outcome_skins(session, skin)
    probability = 1.0 / len(outcome_skins)

    outcome_headers = [
        {"skin_id": o.id, "skin_name": o.name, "steam_url": _steam_listing_url(o)} for o in outcome_skins
    ]

    rows = []
    for wear_name, _lo, _hi in WEAR_BUCKETS:
        cost = _cheapest_ten_cost(skin.id, wear_name, input_source)
        if cost is None:
            input_cell = {"value": None, "color": None}
        else:
            total, oldest = cost
            input_cell = {"value": total, "color": _age_color(oldest)}

        outcome_cells = [
            _outcome_price_cell(
                o.id,
                wear_name,
                legacy_prices_by_skin.get(o.id, {}) if legacy_prices_by_skin is not None else None,
            )
            for o in outcome_skins
        ]

        if input_cell["value"] is None:
            ev_cell = {"value": None}
        else:
            # outcome_cells' values are already net of Steam's sell fee (see
            # _outcome_price_cell) -- no fee applied again here.
            expected_revenue = sum(
                probability * cell["value"] for cell in outcome_cells if cell["value"] is not None
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


# Fetch batches (offers sharing one fetched_at -- see steam_offers_host) contribute
# to the float diagrams' bucketed price estimate below, most-recent-first, capped
# at this many so a skin with a long fetch history doesn't drag in arbitrarily
# stale data -- see build_float_diagram_data.
FLOAT_DIAGRAM_MAX_BATCHES = 20


def build_float_diagram_data(session: Session, skin: Skin, *, input_source: str = DEFAULT_INPUT_SOURCE) -> dict:
    """Raw data for the sidebar's float-vs-price/revenue/EV diagrams
    (webext/float_diagrams.js) -- every individual input-price offer observed
    for `skin` from `input_source` ("steam" or "csfloat", see INPUT_SOURCES)
    across its FLOAT_DIAGRAM_MAX_BATCHES most recent fetch batches (for the
    price-vs-float scatter/bucket chart), plus each relevant skin's own float
    range and per-wear net outcome price (for the input-float -> output-
    revenue curve). The bucketing/weighting/EV math itself lives client-side
    (it's cheap, pure, and only needed for rendering) -- this just gathers
    exactly what that math needs, in one shape. Raises MonoTradeTableError
    under the same conditions as build_table (they share
    _resolve_outcome_skins), since a skin that isn't a usable trade-up input
    has no revenue/EV curve to plot either.
    """
    outcome_skins = _resolve_outcome_skins(session, skin)
    probability = 1.0 / len(outcome_skins)

    if input_source == "csfloat":
        # buy_now only -- see _offers_for_wear's docstring for why an
        # auction's displayed price (just the current bid, not a price
        # actually payable right now) has no place in this diagram either.
        offers = [
            o
            for o in signals.read_market_offers(skin.id)
            if o.source == "csfloat" and o.float_value is not None and o.listing_type == "buy_now"
        ]
    else:
        offers = [o for o in signals.read_steam_offers(skin.id) if o.float_value is not None]
    batch_times = sorted({o.fetched_at for o in offers}, reverse=True)[:FLOAT_DIAGRAM_MAX_BATCHES]
    batch_rank = {fetched_at: rank for rank, fetched_at in enumerate(batch_times)}  # 0 == most recent
    offer_points = [
        {
            "float_value": o.float_value,
            "price": o.price,
            "batch_rank": batch_rank[o.fetched_at],
            # Same purple/green/stale age scheme as the sidebar table's cells
            # (_age_color), just with "red" for the stale tier instead of
            # "orange" -- see build_float_diagram_data's scatter dots.
            "color": _age_color(o.fetched_at, stale="red"),
        }
        for o in offers
        if o.fetched_at in batch_rank
    ]

    def _outcome_entry(outcome: Skin) -> dict:
        # Same pricing.net_sell_price_for_wear every other outcome-price
        # consumer uses (the sidebar table, Construct Contract) -- a
        # buy-order price wins when one's on disk, same as there.
        net_price_by_wear = {}
        for wear_name, _lo, _hi in WEAR_BUCKETS:
            resolved = pricing.net_sell_price_for_wear(outcome.id, wear_name)
            if resolved is not None:
                net_price_by_wear[wear_name] = resolved[0]
        return {
            "skin_id": outcome.id,
            "skin_name": outcome.name,
            "min_float": outcome.min_float if outcome.min_float is not None else 0.0,
            "max_float": outcome.max_float if outcome.max_float is not None else 1.0,
            "probability": probability,
            "net_price_by_wear": net_price_by_wear,
        }

    return {
        "input_skin": {
            "skin_id": skin.id,
            "min_float": skin.min_float if skin.min_float is not None else 0.0,
            "max_float": skin.max_float if skin.max_float is not None else 1.0,
        },
        "offer_points": offer_points,
        "outcomes": [_outcome_entry(o) for o in outcome_skins],
        "wear_buckets": [{"wear_name": name, "lo": lo, "hi": hi} for name, lo, hi in WEAR_BUCKETS],
    }
