"""Client for the CS2Cap prices API (https://docs.cs2cap.com/api-reference/prices).

Free tier: GET /prices, one item per request. Starter+ (config.CS2CAP_PREMIUM_TIER):
POST /prices/batch, up to 100 items per request grouped by item_id -- collapses a
skin's 5 wear-bucket lookups into one call, and makes refetching the whole catalog
(run_bulk_price_import) affordable. The API has no endpoint that reports a key's own
tier, so which path to use is decided entirely by that config flag.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, contracts as contracts_module, pricing, signals
from .market_names import market_hash_name
from .models import Contract, Skin
from .tradeup import WEAR_BUCKETS

BASE_URL = "https://api.cs2c.app/v1"

# POST /prices/batch caps a single request at 100 items (item_ids + market_hash_names
# combined), per the CS2Cap docs.
BATCH_MAX_ITEMS = 100


class Cs2capAPIError(RuntimeError):
    """A request to CS2Cap failed. Carries the HTTP status when there was a response."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


class Cs2capRateLimitError(Cs2capAPIError):
    """A request hit CS2Cap's per-minute rate limit (HTTP 429). Carries the
    Retry-After value (seconds) when the response included one, so a bulk-import
    loop can back off and resume instead of aborting."""

    def __init__(self, retry_after: float | None):
        super().__init__(429, "CS2Cap API rate limit exceeded (429)")
        self.retry_after = retry_after


@dataclass
class PriceImportResult:
    requests_made: int
    observations: int
    # wear buckets for which CS2Cap returned no price data at all
    wears_not_found: int
    # Set if a request failed partway through (e.g. rate limit). Whatever was fetched
    # before the failure is still committed — this just explains why the run stopped early.
    error: str | None = None


def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
    if exc.code == 429:
        retry_after = exc.headers.get("Retry-After")
        raise Cs2capRateLimitError(float(retry_after) if retry_after else None) from exc
    detail = exc.read().decode(errors="replace").strip() or exc.reason
    raise Cs2capAPIError(exc.code, f"CS2Cap API returned {exc.code}: {detail}") from exc


def _fetch_prices(market_hash_name: str, phase: str | None, currency: str) -> dict:
    params = {"market_hash_name": market_hash_name, "currency": currency}
    if phase:
        params["phase"] = phase
    url = f"{BASE_URL}/prices?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.CS2CAP_API_KEY}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        _raise_for_http_error(exc)
    except urllib.error.URLError as exc:
        raise Cs2capAPIError(None, f"Could not reach CS2Cap API: {exc.reason}") from exc


def _fetch_prices_batch(
    *, item_ids: list[int] | None = None, market_hash_names: list[str] | None = None, currency: str = "USD"
) -> dict:
    """POST /prices/batch (Starter+) -- up to BATCH_MAX_ITEMS items per call, mixing
    item_ids and market_hash_names freely; results are matched back by whichever
    identifier the caller supplied. Raises Cs2capRateLimitError on 429 so callers doing
    many batches (run_bulk_price_import) can back off instead of aborting the whole run.
    """
    body: dict = {"currency": currency}
    if item_ids:
        body["item_ids"] = item_ids
    if market_hash_names:
        body["market_hash_names"] = market_hash_names
    request = urllib.request.Request(
        f"{BASE_URL}/prices/batch",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {config.CS2CAP_API_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        _raise_for_http_error(exc)
    except urllib.error.URLError as exc:
        raise Cs2capAPIError(None, f"Could not reach CS2Cap API: {exc.reason}") from exc


def _resolve_item_id(name: str, phase: str) -> int | None:
    """Looks up the item_id for a specific Doppler/Gamma phase via GET /items (the
    Catalog API, unmetered on every tier). Needed because /prices/batch's
    market_hash_names field always resolves to the cheapest phase variant --
    identical caveat to /bids/batch -- so phased skins must be batched by item_id
    instead."""
    params = {"market_hash_name": name, "phase": phase, "limit": 1}
    url = f"{BASE_URL}/items?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.CS2CAP_API_KEY}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            items = json.load(response).get("items") or []
    except urllib.error.HTTPError as exc:
        _raise_for_http_error(exc)
    except urllib.error.URLError as exc:
        raise Cs2capAPIError(None, f"Could not reach CS2Cap API: {exc.reason}") from exc
    return items[0]["item_id"] if items else None


def _chunked(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _quote_price(quote: dict) -> float | None:
    decimal_price = quote.get("lowest_ask_decimal")
    if decimal_price is not None:
        return float(decimal_price)
    minor_units = quote.get("lowest_ask")
    return minor_units / 100 if minor_units is not None else None


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parses an API timestamp to a naive UTC datetime — signal timestamps are
    always naive UTC throughout this app (see braindamage.pricing), so a
    timezone-aware value here would silently break comparisons against them."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def run_price_import(session: Session, skin: Skin, currency: str = "USD") -> PriceImportResult:
    """Fetches current prices for `skin` across every standard wear bucket and
    appends them to its price_observations signal file, then recalculates
    Skin.last_price from the refreshed signals. A wear bucket this skin doesn't
    actually have a listing for (not every skin spans all five) is expected and
    simply counted, not treated as an error.

    Dispatches on config.CS2CAP_PREMIUM_TIER: batches all 5 wear buckets into one
    POST /prices/batch call when set (Starter+), otherwise falls back to the
    Free-tier-compatible one GET /prices call per wear bucket.
    """
    if not config.CS2CAP_API_KEY:
        raise RuntimeError("CS2CAP_API_KEY is not set")

    if config.CS2CAP_PREMIUM_TIER:
        result = _run_price_import_batch(skin, currency)
    else:
        result = _run_price_import_sequential(skin, currency)

    pricing.recalculate_last_price(skin)
    session.commit()
    return result


def _run_price_import_sequential(skin: Skin, currency: str) -> PriceImportResult:
    requests_made = 0
    observations: list[signals.PriceObservationSignal] = []
    wears_not_found = 0
    error: str | None = None

    for wear_name, _lo, _hi in WEAR_BUCKETS:
        name = market_hash_name(skin, wear_name)
        try:
            response = _fetch_prices(name, skin.phase, currency)
        except Cs2capAPIError as exc:
            error = str(exc)
            break
        requests_made += 1

        quotes = response.get("items") or []
        if not quotes:
            wears_not_found += 1
            continue

        fetched_at = signals.now_utc()
        observations.extend(_observations_from_quotes(quotes, wear_name, currency, fetched_at))

    signals.append_price_observations(skin.id, observations)

    return PriceImportResult(
        requests_made=requests_made,
        observations=len(observations),
        wears_not_found=wears_not_found,
        error=error,
    )


def _run_price_import_batch(skin: Skin, currency: str) -> PriceImportResult:
    """One POST /prices/batch call for all 5 wear buckets. If `skin.phase` is set,
    first resolves each wear's phase-specific item_id via the Catalog API (see
    _resolve_item_id) and batches those by item_id; any wear that doesn't resolve
    (e.g. a skin whose name happens to match a phase string -- "CZ75-Auto | Emerald"
    -- but isn't an actual Doppler/Gamma item, so the catalog has no such phase for
    it) falls back to a plain market_hash_name lookup in the same call, rather than
    sending an empty batch request."""
    names_by_wear = {wear_name: market_hash_name(skin, wear_name) for wear_name, _lo, _hi in WEAR_BUCKETS}
    requests_made = 0
    item_id_by_wear: dict[str, int] = {}

    if skin.phase:
        for wear_name, name in names_by_wear.items():
            item_id = _resolve_item_id(name, skin.phase)
            requests_made += 1
            if item_id is not None:
                item_id_by_wear[wear_name] = item_id

    name_wears = [wear_name for wear_name in names_by_wear if wear_name not in item_id_by_wear]

    try:
        response = _fetch_prices_batch(
            item_ids=list(item_id_by_wear.values()) or None,
            market_hash_names=[names_by_wear[w] for w in name_wears] or None,
            currency=currency,
        )
        requests_made += 1
    except Cs2capAPIError as exc:
        return PriceImportResult(requests_made=requests_made, observations=0, wears_not_found=0, error=str(exc))

    by_item_id = {item["item_id"]: item for item in response.get("items", [])}
    by_name = {item["market_hash_name"]: item for item in response.get("items", [])}
    items_by_wear = {wear_name: by_item_id.get(item_id) for wear_name, item_id in item_id_by_wear.items()}
    items_by_wear.update({wear_name: by_name.get(names_by_wear[wear_name]) for wear_name in name_wears})

    observations: list[signals.PriceObservationSignal] = []
    wears_not_found = 0
    fetched_at = signals.now_utc()
    for wear_name in names_by_wear:
        item = items_by_wear.get(wear_name)
        quotes = item.get("quotes") if item else None
        if not quotes:
            wears_not_found += 1
            continue
        observations.extend(_observations_from_quotes(quotes, wear_name, currency, fetched_at))

    signals.append_price_observations(skin.id, observations)

    return PriceImportResult(
        requests_made=requests_made,
        observations=len(observations),
        wears_not_found=wears_not_found,
        error=None,
    )


def _observations_from_quotes(
    quotes: list[dict], wear_name: str, currency: str, fetched_at: datetime
) -> list[signals.PriceObservationSignal]:
    observations = []
    for quote in quotes:
        price = _quote_price(quote)
        if price is None:
            continue
        observations.append(
            signals.PriceObservationSignal(
                source="cs2cap",
                wear_name=wear_name,
                price=price,
                currency=currency,
                observed_at=_parse_timestamp(quote.get("timestamp")),
                fetched_at=fetched_at,
                raw=quote,
            )
        )
    return observations


@dataclass
class ContractPriceImportResult:
    contract_id: str
    requests_made: int = 0
    observations: int = 0
    wears_not_found: int = 0
    skins_updated: int = 0
    error: str | None = None


def refresh_contract_prices(
    session: Session,
    contract: Contract,
    currency: str = "USD",
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> ContractPriceImportResult:
    """Parallel to steam_market_api.refresh_contract_prices, but through the
    CS2Cap API the Maintenance page's "Fetch prices for selected" button
    already uses: runs run_price_import for every skin `contract` references
    as an input or a possible output, then re-simulates and upserts
    `contract` itself so its EV/ROI/CVaR reflect the fresh prices.
    """
    skin_ids = contracts_module.referenced_skin_ids(contract)
    skins = [s for s in (session.get(Skin, sid) for sid in skin_ids) if s is not None]
    result = ContractPriceImportResult(contract_id=contract.id)

    total = len(skins)
    for done, skin in enumerate(skins, start=1):
        skin_result = run_price_import(session, skin, currency)
        result.requests_made += skin_result.requests_made
        result.observations += skin_result.observations
        result.wears_not_found += skin_result.wears_not_found
        result.skins_updated += 1
        if skin_result.error and result.error is None:
            result.error = skin_result.error
        if on_progress is not None:
            on_progress(done, total)
        if skin_result.error:
            break

    contracts_module.resimulate(session, contract)

    return result


@dataclass
class BulkPriceImportResult:
    skins_processed: int = 0
    requests_made: int = 0
    observations: int = 0
    # (skin, wear) combos CS2Cap returned no price data for at all
    wears_not_found: int = 0
    # Set if a batch call failed after retries (e.g. sustained rate limiting).
    # Whatever was fetched before the failure is still committed.
    error: str | None = None


def _fetch_batch_with_retry(
    item_ids: list[int], market_hash_names: list[str], currency: str, max_retries: int = 5
) -> dict:
    for attempt in range(max_retries):
        try:
            return _fetch_prices_batch(
                item_ids=item_ids or None, market_hash_names=market_hash_names or None, currency=currency
            )
        except Cs2capRateLimitError as exc:
            if attempt == max_retries - 1:
                raise
            time.sleep(exc.retry_after or 5)
    raise AssertionError("unreachable")  # loop always returns or raises


def run_bulk_price_import(
    session: Session,
    currency: str = "USD",
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> BulkPriceImportResult:
    """Refetches current prices for every normal and StatTrak skin in the catalog
    (Souvenir skins excluded) via as few POST /prices/batch calls as the 100-item
    cap allows -- the Maintenance page's "Refetch all skin prices" action.
    Starter+ only: at ~2,150 skins x 5 wear buckets, the Free tier's one-GET-per-item
    model would take ~10,750 requests; batching brings that down to roughly 100.
    """
    if not config.CS2CAP_PREMIUM_TIER:
        raise RuntimeError("Bulk price import requires CS2CAP_PREMIUM_TIER")
    if not config.CS2CAP_API_KEY:
        raise RuntimeError("CS2CAP_API_KEY is not set")

    skins = list(session.scalars(select(Skin).where(Skin.souvenir.is_(False))).all())

    # (skin, wear_name, market_hash_name, item_id) -- item_id set only for Doppler/Gamma
    # skins, resolved via the Catalog API since /prices/batch's market_hash_names field
    # always resolves to the cheapest phase variant (see _resolve_item_id).
    combos: list[tuple[Skin, str, str, int | None]] = []
    requests_made = 0
    for skin in skins:
        for wear_name, _lo, _hi in WEAR_BUCKETS:
            name = market_hash_name(skin, wear_name)
            item_id = None
            if skin.phase:
                item_id = _resolve_item_id(name, skin.phase)
                requests_made += 1
            combos.append((skin, wear_name, name, item_id))

    item_id_index: dict[int, list[tuple[Skin, str]]] = defaultdict(list)
    name_index: dict[str, list[tuple[Skin, str]]] = defaultdict(list)
    for skin, wear_name, name, item_id in combos:
        if item_id is not None:
            item_id_index[item_id].append((skin, wear_name))
        else:
            name_index[name].append((skin, wear_name))

    observations_by_skin: dict[str, list[signals.PriceObservationSignal]] = defaultdict(list)
    wears_not_found = 0
    error: str | None = None
    total_batches = -(-len(combos) // BATCH_MAX_ITEMS)  # ceil division

    for batch_num, chunk in enumerate(_chunked(combos, BATCH_MAX_ITEMS), start=1):
        item_ids = [item_id for _, _, _, item_id in chunk if item_id is not None]
        names = [name for _, _, name, item_id in chunk if item_id is None]

        try:
            response = _fetch_batch_with_retry(item_ids, names, currency)
        except Cs2capAPIError as exc:
            error = str(exc)
            break
        requests_made += 1

        fetched_at = signals.now_utc()
        matched_keys: set[tuple[str, str]] = set()
        for item in response.get("items", []):
            quotes = item.get("quotes") or []
            if not quotes:
                continue
            targets = item_id_index.get(item["item_id"]) or name_index.get(item["market_hash_name"]) or []
            for skin, wear_name in targets:
                matched_keys.add((skin.id, wear_name))
                observations_by_skin[skin.id].extend(_observations_from_quotes(quotes, wear_name, currency, fetched_at))

        for skin, wear_name, _name, _item_id in chunk:
            if (skin.id, wear_name) not in matched_keys:
                wears_not_found += 1

        if on_progress is not None:
            on_progress(batch_num, total_batches)
        if batch_num < total_batches:
            time.sleep(1.5)  # stay comfortably under Starter's 40 req/min cap

    for skin in skins:
        obs = observations_by_skin.get(skin.id)
        if obs:
            signals.append_price_observations(skin.id, obs)
        pricing.recalculate_last_price(skin)

    session.commit()

    return BulkPriceImportResult(
        skins_processed=len(skins),
        requests_made=requests_made,
        observations=sum(len(v) for v in observations_by_skin.values()),
        wears_not_found=wears_not_found,
        error=error,
    )
