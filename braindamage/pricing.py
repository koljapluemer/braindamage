"""Price resolution against a skin's JSON signal files (see braindamage/signals.py)
rather than a SQL table — the whole point of moving price data to signals is that
this module can grow how it reads/merges sources without a migration.
"""

from __future__ import annotations

from datetime import datetime

from . import signals
from .models import Skin
from .signals import now_utc


def _candidates(skin_id: str, wear_name: str | None) -> list[tuple[float, datetime]]:
    """(price, timestamp) pairs across both signal kinds, optionally filtered to
    one wear. Point-in-time observations use observed_at, falling back to
    fetched_at when the source didn't report one; aggregated buckets use their
    own bucket timestamp and closing price."""
    candidates: list[tuple[float, datetime]] = []

    for obs in signals.read_price_observations(skin_id):
        if wear_name is not None and obs.wear_name != wear_name:
            continue
        candidates.append((obs.price, obs.observed_at or obs.fetched_at))

    for bucket in signals.read_aggregated_prices(skin_id):
        if wear_name is not None and bucket.wear_name != wear_name:
            continue
        if bucket.close is None:
            continue
        candidates.append((bucket.close, bucket.bucket))

    return candidates


def latest_price_for_wear(skin_id: str, wear_name: str) -> tuple[float, datetime] | None:
    """Latest known (price, timestamp) for `skin_id` at `wear_name`, across every
    signal source, or None if there's no price data for that wear at all."""
    candidates = _candidates(skin_id, wear_name)
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[1])


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
