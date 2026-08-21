"""Price resolution against a skin's JSON signal files (see braindamage/signals.py)
rather than a SQL table — the whole point of moving price data to signals is that
this module can grow how it reads/merges sources without a migration.
"""

from __future__ import annotations

from datetime import datetime

from . import signals, steam_fees
from .models import Skin
from .signals import now_utc


def _all_candidates(skin_id: str) -> list[tuple[str | None, float, datetime]]:
    """(wear_name, price, timestamp) for every price signal of `skin_id`, across
    both signal kinds -- one disk read per kind, shared by every wear-filtered
    or wear-grouped lookup below so repeated per-wear calls don't each
    re-read+re-validate the same JSON files. Point-in-time observations use
    observed_at, falling back to fetched_at when the source didn't report one;
    aggregated buckets use their own bucket timestamp and closing price."""
    candidates: list[tuple[str | None, float, datetime]] = []

    for obs in signals.read_price_observations(skin_id):
        candidates.append((obs.wear_name, obs.price, obs.observed_at or obs.fetched_at))

    for bucket in signals.read_aggregated_prices(skin_id):
        if bucket.close is None:
            continue
        candidates.append((bucket.wear_name, bucket.close, bucket.bucket))

    return candidates


def _candidates(skin_id: str, wear_name: str | None) -> list[tuple[float, datetime]]:
    """(price, timestamp) pairs, optionally filtered to one wear."""
    return [
        (price, ts) for wear, price, ts in _all_candidates(skin_id) if wear_name is None or wear == wear_name
    ]


def latest_price_for_wear(skin_id: str, wear_name: str) -> tuple[float, datetime] | None:
    """Latest known (price, timestamp) for `skin_id` at `wear_name`, across every
    signal source, or None if there's no price data for that wear at all."""
    candidates = _candidates(skin_id, wear_name)
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[1])


def latest_prices_by_wear(skin_id: str) -> dict[str, tuple[float, datetime]]:
    """Latest (price, timestamp) per wear bucket for `skin_id`, from one read of
    its signal files. For a caller that needs every wear's latest price (e.g.
    sampling many hypothetical average floats for the EV-vs-float curve),
    this is one file read total instead of one per wear per sample."""
    best: dict[str, tuple[float, datetime]] = {}
    for wear, price, ts in _all_candidates(skin_id):
        if wear is None:
            continue
        current = best.get(wear)
        if current is None or ts > current[1]:
            best[wear] = (price, ts)
    return best


def latest_buy_order_for_wear(skin_id: str, wear_name: str) -> tuple[float, datetime, int] | None:
    """Latest (price, fetched_at, num_orders) buy-order-book summary for
    `skin_id` at `wear_name`, or None if there isn't one -- see
    signals.BuyOrderSummarySignal. Deliberately kept separate from
    latest_price_for_wear/_all_candidates: a buy-order price is a distinct,
    more specific signal (what a seller could get RIGHT NOW without waiting
    on a listing to sell), not folded into the general last-price resolution
    the rest of the app (contract simulation, Skin.last_price) relies on."""
    entries = [e for e in signals.read_buy_order_summaries(skin_id) if e.wear_name == wear_name]
    if not entries:
        return None
    latest = max(entries, key=lambda e: e.fetched_at)
    return latest.price, latest.fetched_at, latest.num_orders


def net_sell_price_for_wear(
    skin_id: str,
    wear_name: str,
    *,
    legacy_prices: dict[str, tuple[float, datetime]] | None = None,
) -> tuple[float, datetime, str] | None:
    """The single, shared definition of "outcome net sell price" -- what a
    seller would actually walk away with fulfilling a price on Steam
    Community Market right now, after Steam's real per-sale fee
    (braindamage.steam_fees). Every consumer that prices a mono trade-up's
    *outcome* skin (the sidebar table, its float/EV diagrams, and the
    Construct Contract combo search, for both Steam and CSFloat) MUST go
    through this, not its own independently-computed version -- those used
    to disagree (the table preferred a buy-order price while Construct
    Contract silently ignored buy orders entirely and used a plain last-
    price instead), which is exactly the kind of drift this function exists
    to make impossible.

    A buy-order-book price (latest_buy_order_for_wear) wins whenever one's
    on disk -- an instant sale, no listing needed, so it's the best price
    actually obtainable right now. Otherwise `legacy_prices[wear_name]` if
    `legacy_prices` was given (the one-time overview snapshot -- see
    mono_trade_overview, which must never fall through to the full
    historical pricing reader for its naive-EV priming pass), else the
    general last-price resolution (latest_price_for_wear).

    Returns (net_price, observed_at, source) where source is "buy_order" or
    "fallback" -- callers that color-code by source/freshness (the sidebar
    table) key off `source`; those that only need the number (Construct
    Contract, the float diagrams) can ignore it. None if there's no price
    for this wear at all.
    """
    buy_order = latest_buy_order_for_wear(skin_id, wear_name)
    if buy_order is not None:
        price, fetched_at, _num_orders = buy_order
        return steam_fees.net_proceeds(price), fetched_at, "buy_order"

    price_info = (
        legacy_prices.get(wear_name) if legacy_prices is not None else latest_price_for_wear(skin_id, wear_name)
    )
    if price_info is None:
        return None
    price, observed_at = price_info
    return steam_fees.net_proceeds(price), observed_at, "fallback"


def recalculate_last_price(skin: Skin) -> None:
    """Refreshes `skin.last_price` and its timestamps from this skin's own signal
    files (latest observation across all wears combined). Mutates `skin` in
    place — the caller commits.
    """
    candidates = _candidates(skin.id, wear_name=None)
    if candidates:
        price, recency = max(candidates, key=lambda c: c[1])
        skin.last_price = price
        skin.last_price_calculation_data_point_recency = recency
    else:
        skin.last_price = None
        skin.last_price_calculation_data_point_recency = None
    skin.last_price_recalculated_at = now_utc()
