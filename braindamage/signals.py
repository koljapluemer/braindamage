"""Per-skin JSON signal files: the flexible, strictly-typed alternative to a SQL
table for every new price/event data shape that shows up.

Each Skin gets a folder at ``SKINS_DIR / <skin_id> /`` holding one JSON file per
*kind* of signal — ``price_observations.json``, ``aggregated_prices.json``,
``events.json`` — rather than one combined blob. That keeps each write small and
each kind independently typed and extensible: a new signal kind is a new file and a
new Pydantic model, never a migration.

``braindamage.pricing`` reads these to resolve "latest price for this skin at this
wear" and to recalculate ``Skin.last_price`` et al. Nothing else in the app should
read or write these files directly — go through this module so the on-disk shape
stays centralized.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from .db import DATA_DIR

SKINS_DIR = DATA_DIR / "skins"


def now_utc() -> datetime:
    """The current time as a naive UTC datetime — every timestamp in this app
    (signal entries, Skin.last_price_recalculated_at, Contract timestamps) is
    naive UTC by convention, so naive/aware datetimes never get compared
    against each other and raise."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PriceObservationSignal(BaseModel):
    """One point-in-time price reading for a skin at a specific wear, from a
    source. Append-only — entries are never edited or removed, only added."""

    source: str
    wear_name: str | None = None
    price: float
    currency: str = "USD"
    observed_at: datetime | None = None
    fetched_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class AggregatedPriceSignal(BaseModel):
    """One OHLC-style hourly price bucket for a skin at a specific wear, from a
    bulk historical source (e.g. the cs2.sh dataset)."""

    source: str
    wear_name: str | None = None
    bucket: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


class SkinEvent(BaseModel):
    """Reserved for future non-price signals (Valve announcements, streamer
    callouts, etc.). The shape is intentionally minimal — no writer exists yet."""

    source: str
    kind: str
    occurred_at: datetime
    summary: str
    raw: dict[str, Any] = Field(default_factory=dict)


_PRICE_OBSERVATIONS_FILE = "price_observations.json"
_AGGREGATED_PRICES_FILE = "aggregated_prices.json"
_EVENTS_FILE = "events.json"

_price_observations_adapter = TypeAdapter(list[PriceObservationSignal])
_aggregated_prices_adapter = TypeAdapter(list[AggregatedPriceSignal])
_events_adapter = TypeAdapter(list[SkinEvent])


def _read(skin_id: str, filename: str, adapter: TypeAdapter) -> list:
    path = SKINS_DIR / skin_id / filename
    if not path.exists():
        return []
    return adapter.validate_json(path.read_text(encoding="utf-8"))


def _write(skin_id: str, filename: str, adapter: TypeAdapter, items: list) -> None:
    skin_dir = SKINS_DIR / skin_id
    skin_dir.mkdir(parents=True, exist_ok=True)
    (skin_dir / filename).write_bytes(adapter.dump_json(items, indent=2))


def read_price_observations(skin_id: str) -> list[PriceObservationSignal]:
    return _read(skin_id, _PRICE_OBSERVATIONS_FILE, _price_observations_adapter)


def append_price_observations(skin_id: str, new_observations: list[PriceObservationSignal]) -> None:
    if not new_observations:
        return
    existing = read_price_observations(skin_id)
    existing.extend(new_observations)
    _write(skin_id, _PRICE_OBSERVATIONS_FILE, _price_observations_adapter, existing)


def read_aggregated_prices(skin_id: str) -> list[AggregatedPriceSignal]:
    return _read(skin_id, _AGGREGATED_PRICES_FILE, _aggregated_prices_adapter)


def append_aggregated_prices(skin_id: str, new_prices: list[AggregatedPriceSignal]) -> None:
    if not new_prices:
        return
    existing = read_aggregated_prices(skin_id)
    existing.extend(new_prices)
    _write(skin_id, _AGGREGATED_PRICES_FILE, _aggregated_prices_adapter, existing)


def read_events(skin_id: str) -> list[SkinEvent]:
    return _read(skin_id, _EVENTS_FILE, _events_adapter)
