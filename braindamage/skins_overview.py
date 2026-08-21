"""Data for the browser extension's "Skins" tab (webext/skins.js): one compact
line per normal (non-StatTrak, non-Souvenir) catalog skin, grouped by
collection then rarity. Unlike braindamage.mono_trade_overview (one row per
valid mono-trade, EV-focused), this lists every normal skin regardless of
trade-up eligibility -- ineligible skins (Covert, orphaned collection,
knife/glove) simply carry no group-average price. Read-only, like that
module: makes no network calls and writes nothing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mono_trade_table, pricing, signals
from .models import Skin
from .tradeup import RARITY_LADDER, WEAR_BUCKETS

_UNKNOWN_COLLECTION_ID = "__no_collection__"
_UNKNOWN_COLLECTION_NAME = "Unknown collection"
_UNKNOWN_RARITY_NAME = "Unknown rarity"

# Ladder position for sorting rarity groups within a collection -- anything
# not on the ladder (e.g. "Contraband") sorts after every ladder rarity.
_RARITY_ORDER = {name: rank for rank, (name, _color) in enumerate(RARITY_LADDER)}

_LegacyPrices = dict[str, tuple[float, datetime]]


def _legacy_prices(skin_id: str, cache: dict[str, _LegacyPrices]) -> _LegacyPrices:
    """Same one-read-per-skin memoized legacy-snapshot lookup as
    mono_trade_overview.build_overview's own `legacy_prices` closure -- reused
    here (not imported from there) since it's small and this module has its
    own cache dict shape. Never falls through to the full historical pricing
    reader, so this stays cheap across the whole catalog."""
    if skin_id not in cache:
        snapshot = signals.read_legacy_price_snapshot(skin_id)
        cache[skin_id] = {
            wear: (entry.price, entry.observed_at)
            for wear, entry in (snapshot.prices_by_wear.items() if snapshot else [])
        }
    return cache[skin_id]


def _snapshot_color(skin_id: str, source: str) -> str:
    """purple/green/orange (mono_trade_table._age_color) keyed off the most
    recent COMPREHENSIVE order-book snapshot for `skin_id` from `source`
    ("steam" or "csfloat") -- see SteamOfferSignal.comprehensive /
    MarketOfferSignal.comprehensive, written by the sidebar's "Auto-Scroll &
    Save". "grey" if no comprehensive snapshot has ever been saved."""
    if source == "csfloat":
        fetch_times = [
            o.fetched_at for o in signals.read_market_offers(skin_id) if o.source == "csfloat" and o.comprehensive
        ]
    else:
        fetch_times = [o.fetched_at for o in signals.read_steam_offers(skin_id) if o.comprehensive]
    if not fetch_times:
        return "grey"
    return mono_trade_table._age_color(max(fetch_times))


def _own_wear_cell(skin_id: str, wear_name: str, legacy: _LegacyPrices) -> dict:
    """This skin's own buy-order price at `wear_name`, deliberately WITHOUT
    Steam's sell fee applied (unlike pricing.net_sell_price_for_wear, which
    nets an *outcome* skin's proceeds) -- a buy order is what a buyer commits
    to pay outright, not something to net a seller fee off of here. Colored
    by fetch age when a real buy-order summary is on disk, else "grey" for a
    legacy-snapshot fallback (same fallback-is-always-grey convention as
    mono_trade_table._outcome_price_cell)."""
    buy_order = pricing.latest_buy_order_for_wear(skin_id, wear_name)
    if buy_order is not None:
        price, fetched_at, _num_orders = buy_order
        return {"wear_name": wear_name, "value": price, "color": mono_trade_table._age_color(fetched_at)}
    legacy_entry = legacy.get(wear_name)
    if legacy_entry is None:
        return {"wear_name": wear_name, "value": None, "color": None}
    price, _observed_at = legacy_entry
    return {"wear_name": wear_name, "value": price, "color": "grey"}


def _group_avg_prices(
    session: Session,
    skin: Skin,
    cache: dict[tuple[str, str], tuple[float | None, float | None]],
    legacy_cache: dict[str, _LegacyPrices],
) -> tuple[float | None, float | None]:
    """(avg Battle-Scarred, avg Factory New) net sell price -- Steam sell fee
    included, via pricing.net_sell_price_for_wear -- across the mono-trade
    outcome group `skin` would land in (mono_trade_table._resolve_outcome_skins),
    averaged with equal weight per outcome skin (mirroring build_table's own
    per-row EV, which weights every outcome skin equally). (None, None) if
    `skin` isn't a usable trade-up input at all.

    Memoized by (collection_id, rarity_name): every normal skin sharing a
    collection+rarity that IS a valid weapon input resolves to the exact
    same outcome group, so this is computed once per group, not once per
    skin -- essential for a catalog-wide page like this one. The ineligible-
    category check runs before the cache lookup so a knife/glove sharing a
    ladder rarity name with real weapon skins in the same collection never
    poisons (or is poisoned by) that group's cached result.
    """
    if skin.category_name in mono_trade_table._NON_WEAPON_CATEGORIES or skin.souvenir or skin.collection_id is None:
        return None, None

    key = (skin.collection_id, skin.rarity_name)
    if key not in cache:
        try:
            outcome_skins = mono_trade_table._resolve_outcome_skins(session, skin)
        except mono_trade_table.MonoTradeTableError:
            cache[key] = (None, None)
        else:

            def _avg(wear_name: str) -> float | None:
                values = [
                    resolved[0]
                    for outcome in outcome_skins
                    if (
                        resolved := pricing.net_sell_price_for_wear(
                            outcome.id, wear_name, legacy_prices=_legacy_prices(outcome.id, legacy_cache)
                        )
                    )
                    is not None
                ]
                return sum(values) / len(values) if values else None

            cache[key] = (_avg("Battle-Scarred"), _avg("Factory New"))
    return cache[key]


def build_skins_overview(session: Session) -> dict:
    """Every normal (non-StatTrak, non-Souvenir) catalog skin, grouped by
    collection then rarity (ladder order, unrecognized rarities last),
    skins sorted by name within each group -- the full data set for
    webext/skins.js's "Skins" tab."""
    skins = list(
        session.scalars(
            select(Skin).where(Skin.stattrak.is_(False), Skin.souvenir.is_(False)).order_by(Skin.name)
        ).all()
    )

    legacy_cache: dict[str, _LegacyPrices] = {}
    group_avg_cache: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    collections: dict[str, dict] = {}

    for skin in skins:
        collection_id = skin.collection_id or _UNKNOWN_COLLECTION_ID
        collection = collections.setdefault(
            collection_id,
            {
                "collection_id": collection_id,
                "collection_name": skin.collection_name or _UNKNOWN_COLLECTION_NAME,
                "rarities": {},
            },
        )

        rarity_name = skin.rarity_name or _UNKNOWN_RARITY_NAME
        rarity = collection["rarities"].setdefault(
            rarity_name, {"rarity_name": rarity_name, "avg_bs": None, "avg_fn": None, "skins": []}
        )
        rarity["avg_bs"], rarity["avg_fn"] = _group_avg_prices(session, skin, group_avg_cache, legacy_cache)

        legacy = _legacy_prices(skin.id, legacy_cache)
        rarity["skins"].append(
            {
                "skin_id": skin.id,
                "skin_name": skin.name,
                "steam_url": mono_trade_table._steam_listing_url(skin),
                "steam_snapshot_color": _snapshot_color(skin.id, "steam"),
                "csfloat_snapshot_color": _snapshot_color(skin.id, "csfloat"),
                "wears": [_own_wear_cell(skin.id, wear_name, legacy) for wear_name, _lo, _hi in WEAR_BUCKETS],
            }
        )

    result_collections = []
    for collection in collections.values():
        rarities = sorted(
            collection["rarities"].values(),
            key=lambda r: (_RARITY_ORDER.get(r["rarity_name"], len(RARITY_LADDER)), r["rarity_name"]),
        )
        result_collections.append(
            {
                "collection_id": collection["collection_id"],
                "collection_name": collection["collection_name"],
                "rarities": rarities,
            }
        )
    result_collections.sort(key=lambda c: c["collection_name"])

    return {"ok": True, "collections": result_collections}
