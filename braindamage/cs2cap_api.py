"""Client for the CS2Cap prices API (https://docs.cs2cap.com/api-reference/prices).

Uses GET /prices (one item per request), not POST /prices/batch — batch lookup requires
a Starter+ subscription; GET /prices is the endpoint available on the Free tier.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import config, contracts as contracts_module, pricing, signals
from .market_names import market_hash_name
from .models import Contract, Skin
from .tradeup import WEAR_BUCKETS

BASE_URL = "https://api.cs2c.app/v1"


class Cs2capAPIError(RuntimeError):
    """A request to CS2Cap failed. Carries the HTTP status when there was a response."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PriceImportResult:
    requests_made: int
    observations: int
    # wear buckets for which CS2Cap returned no price data at all
    wears_not_found: int
    # Set if a request failed partway through (e.g. rate limit). Whatever was fetched
    # before the failure is still committed — this just explains why the run stopped early.
    error: str | None = None


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
        detail = exc.read().decode(errors="replace").strip() or exc.reason
        raise Cs2capAPIError(exc.code, f"CS2Cap API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise Cs2capAPIError(None, f"Could not reach CS2Cap API: {exc.reason}") from exc


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
    simply counted, not treated as an error."""
    if not config.CS2CAP_API_KEY:
        raise RuntimeError("CS2CAP_API_KEY is not set")

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

    signals.append_price_observations(skin.id, observations)
    pricing.recalculate_last_price(skin)
    session.commit()

    return PriceImportResult(
        requests_made=requests_made,
        observations=len(observations),
        wears_not_found=wears_not_found,
        error=error,
    )


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
