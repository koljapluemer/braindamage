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


class MarketOfferSignal(BaseModel):
    """One individual, currently-live floated marketplace listing observed for
    a skin -- unlike PriceObservationSignal (one point-in-time price for a
    whole wear bucket), this captures a single buyable listing's own exact
    float and price. Written by braindamage.postvalidate when it checks
    whether enough real listings exist within a specific float sub-range to
    actually execute a trade-up's buying plan. Append-only, and kept purely as
    a historical record for later float-vs-price correlation analysis --
    braindamage.pricing never reads this file back."""

    source: str
    listing_id: str
    market_hash_name: str
    wear_name: str | None = None
    float_value: float | None = None
    price: float
    currency: str = "USD"
    listing_type: str
    fetched_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class SteamOfferSignal(BaseModel):
    """One individual listing observed on a Steam Community Market item
    listing page -- unlike MarketOfferSignal (CSFloat, has a real listing_id
    and listing_type), Steam's page exposes no listing ID and everything
    rendered there is buy-now by construction, so (float_value, pattern_seed,
    price) stands in as a synthetic dedup identity instead (see
    braindamage.steam_offer_combos). Append-only, written by the
    steam_offers_host native-messaging host, one skin_id folder at a time --
    the same skin_id scoping as MarketOfferSignal's market_offers.json."""

    source: str = "steam"
    market_hash_name: str
    wear_name: str | None = None
    float_value: float | None = None
    pattern_seed: int | None = None
    price: float
    currency: str = "USD"
    fetched_at: datetime
    # True for a batch written by the sidebar's "Auto-Scroll & Save" flow
    # (webext/sidebar.js), which scrolls the listing page to load as many
    # offers as Steam will render -- up to 1000 -- before saving, instead of
    # whatever ~20-row window happened to be on screen. Lets downstream
    # consumers tell a near-complete snapshot of a skin's listings apart from
    # an ordinary single-page scrape. False for every other write path.
    comprehensive: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class BuyOrderSummarySignal(BaseModel):
    """One point-in-time summary of a skin's buy-order book at a specific
    wear, scraped from a Steam Community Market listing page's "N requests to
    buy at $X or lower" line -- Steam only renders that line once a wear
    filter is active on the page, so unlike SteamOfferSignal (one row per
    individual sell listing) this is wear-scoped by construction. This is the
    buy side of the order book (an instant, no-listing-needed sell price),
    the opposite of SteamOfferSignal's sell-side listings. Append-only,
    written by the steam_offers_host native-messaging host."""

    source: str = "steam"
    market_hash_name: str
    wear_name: str
    price: float
    currency: str = "USD"
    num_orders: int
    fetched_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class ContractHistorySignal(BaseModel):
    """One "Construct Contract" result recorded for a skin -- just enough to
    render the sidebar's per-skin contract history list (date, EV, raw input
    float), not the full contract (the underlying listings are already
    persisted separately as SteamOfferSignal entries). Append-only, written
    by the steam_offers_host native-messaging host right after a contract is
    successfully constructed."""

    expected_value: float
    raw_avg_float: float
    generated_at: datetime


class LegacyWearPrice(BaseModel):
    """One latest legacy price retained by the one-time overview snapshot."""

    price: float
    observed_at: datetime


class LegacyPriceSnapshot(BaseModel):
    """Compact replacement for scanning a skin's full legacy price history."""

    generated_at: datetime
    prices_by_wear: dict[str, LegacyWearPrice] = Field(default_factory=dict)


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
_MARKET_OFFERS_FILE = "market_offers.json"
_STEAM_OFFERS_FILE = "steam_offers.json"
_BUY_ORDER_SUMMARY_FILE = "buy_order_summary.json"
_CONTRACT_HISTORY_FILE = "contract_history.json"
_LEGACY_PRICE_SNAPSHOT_FILE = "legacy_latest_prices.json"
_EVENTS_FILE = "events.json"

_price_observations_adapter = TypeAdapter(list[PriceObservationSignal])
_aggregated_prices_adapter = TypeAdapter(list[AggregatedPriceSignal])
_market_offers_adapter = TypeAdapter(list[MarketOfferSignal])
_steam_offers_adapter = TypeAdapter(list[SteamOfferSignal])
_buy_order_summary_adapter = TypeAdapter(list[BuyOrderSummarySignal])
_contract_history_adapter = TypeAdapter(list[ContractHistorySignal])
_legacy_price_snapshot_adapter = TypeAdapter(LegacyPriceSnapshot)
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


def read_market_offers(skin_id: str) -> list[MarketOfferSignal]:
    return _read(skin_id, _MARKET_OFFERS_FILE, _market_offers_adapter)


def append_market_offers(skin_id: str, new_offers: list[MarketOfferSignal]) -> None:
    if not new_offers:
        return
    existing = read_market_offers(skin_id)
    existing.extend(new_offers)
    _write(skin_id, _MARKET_OFFERS_FILE, _market_offers_adapter, existing)


def read_steam_offers(skin_id: str) -> list[SteamOfferSignal]:
    return _read(skin_id, _STEAM_OFFERS_FILE, _steam_offers_adapter)


def append_steam_offers(skin_id: str, new_offers: list[SteamOfferSignal]) -> None:
    if not new_offers:
        return
    existing = read_steam_offers(skin_id)
    existing.extend(new_offers)
    _write(skin_id, _STEAM_OFFERS_FILE, _steam_offers_adapter, existing)


def read_buy_order_summaries(skin_id: str) -> list[BuyOrderSummarySignal]:
    return _read(skin_id, _BUY_ORDER_SUMMARY_FILE, _buy_order_summary_adapter)


def append_buy_order_summaries(skin_id: str, new_summaries: list[BuyOrderSummarySignal]) -> None:
    if not new_summaries:
        return
    existing = read_buy_order_summaries(skin_id)
    existing.extend(new_summaries)
    _write(skin_id, _BUY_ORDER_SUMMARY_FILE, _buy_order_summary_adapter, existing)


def read_contract_history(skin_id: str) -> list[ContractHistorySignal]:
    return _read(skin_id, _CONTRACT_HISTORY_FILE, _contract_history_adapter)


def append_contract_history(skin_id: str, new_entries: list[ContractHistorySignal]) -> None:
    if not new_entries:
        return
    existing = read_contract_history(skin_id)
    existing.extend(new_entries)
    _write(skin_id, _CONTRACT_HISTORY_FILE, _contract_history_adapter, existing)


def read_legacy_price_snapshot(skin_id: str) -> LegacyPriceSnapshot | None:
    path = SKINS_DIR / skin_id / _LEGACY_PRICE_SNAPSHOT_FILE
    if not path.exists():
        return None
    return _legacy_price_snapshot_adapter.validate_json(path.read_text(encoding="utf-8"))


def write_legacy_price_snapshot(skin_id: str, snapshot: LegacyPriceSnapshot) -> None:
    skin_dir = SKINS_DIR / skin_id
    skin_dir.mkdir(parents=True, exist_ok=True)
    path = skin_dir / _LEGACY_PRICE_SNAPSHOT_FILE
    path.write_bytes(_legacy_price_snapshot_adapter.dump_json(snapshot, indent=2))


def read_events(skin_id: str) -> list[SkinEvent]:
    return _read(skin_id, _EVENTS_FILE, _events_adapter)
