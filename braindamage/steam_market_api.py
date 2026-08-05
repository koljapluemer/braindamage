"""Client for Steam Community Market's public priceoverview endpoint
(https://steamcommunity.com/market/priceoverview/) -- unlike cs2cap_api, this
needs no API key and no login session (confirmed by hand: the sibling
/market/pricehistory/ endpoint 400s without a steamLoginSecure cookie, but
this one returns 200 straight away for an anonymous request). It only gives a
live snapshot (lowest/median price + volume), not a history, so it's used the
same way cs2cap_api is: as a per-wear price observation appended to a skin's
signal file, not a bulk backfill.

Valve rate-limits Steam Community Market endpoints hard and by IP -- reports
in the wild describe 429s escalating to multi-hour IP-wide cooldowns. Every
request made through this module is paced by REQUEST_INTERVAL_SECONDS,
shared across an entire run (not reset per skin), to stay well under that.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import contracts as contracts_module
from . import pricing, signals
from .market_names import market_hash_name
from .models import Contract, Skin
from .tradeup import WEAR_BUCKETS

BASE_URL = "https://steamcommunity.com/market/priceoverview/"
APPID = 730  # CS2 -- the only game this app models
CURRENCY = "1"  # Steam's USD currency code; signals/pricing assume USD throughout

REQUEST_INTERVAL_SECONDS = 2.0

_PRICE_RE = re.compile(r"[\d.]+")


class SteamMarketAPIError(RuntimeError):
    """A request to Steam's priceoverview endpoint failed. Carries the HTTP
    status when there was a response."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class SteamPriceRefreshResult:
    contract_id: str
    requests_made: int = 0
    observations: int = 0
    wears_not_found: int = 0
    skins_updated: int = 0
    # Set if a request failed partway through (e.g. rate limit). Whatever was
    # fetched before the failure is still committed, and the contract is still
    # re-simulated from whatever prices are now on disk.
    error: str | None = None


class _RateLimiter:
    """Sleeps as needed so consecutive wait() calls are never closer together
    than `min_interval` seconds."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            remaining = self._min_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


def _fetch_price_overview(name: str) -> dict:
    params = {"appid": APPID, "currency": CURRENCY, "market_hash_name": name}
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip() or exc.reason
        raise SteamMarketAPIError(exc.code, f"Steam Market API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SteamMarketAPIError(None, f"Could not reach Steam Market API: {exc.reason}") from exc


# Valve's actual per-IP quota for this endpoint isn't documented, so
# REQUEST_INTERVAL_SECONDS is a guess -- a 429 mid-run more likely means we've
# hit a short rolling-window limit than a hard multi-hour ban, so it's worth
# backing off and retrying a few times before giving up on the whole run.
_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0, 120.0)


def _fetch_with_retry(name: str) -> dict:
    last_error: SteamMarketAPIError | None = None
    for backoff in (0.0, *_RETRY_BACKOFF_SECONDS):
        if backoff:
            time.sleep(backoff)
        try:
            return _fetch_price_overview(name)
        except SteamMarketAPIError as exc:
            if exc.status_code != 429:
                raise
            last_error = exc
    assert last_error is not None
    raise last_error


def _median_price(response: dict) -> float | None:
    """The median sale price from a priceoverview response, falling back to
    the lowest current listing price for thin-volume items that only carry
    that field. None if the item has no market data at all (success: false,
    or a bare {"success": true} with neither field present)."""
    if not response.get("success"):
        return None
    raw = response.get("median_price") or response.get("lowest_price")
    if raw is None:
        return None
    match = _PRICE_RE.search(raw.replace(",", ""))
    if match is None:
        return None
    return float(match.group())


def refresh_contract_prices(
    session: Session,
    contract: Contract,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> SteamPriceRefreshResult:
    """Refetches Steam prices (all five wear buckets) for every skin
    `contract` references as an input or a possible output, appends them to
    each skin's price_observations signal file, refreshes each Skin's
    last_price, and then re-simulates and upserts `contract` itself so its
    EV/ROI/CVaR reflect the fresh prices.
    """
    skin_ids = contracts_module.referenced_skin_ids(contract)
    skins = [s for s in (session.get(Skin, sid) for sid in skin_ids) if s is not None]
    result = SteamPriceRefreshResult(contract_id=contract.id)

    total_steps = len(skins) * len(WEAR_BUCKETS)
    done_steps = 0
    limiter = _RateLimiter(REQUEST_INTERVAL_SECONDS)

    for skin in skins:
        observations: list[signals.PriceObservationSignal] = []
        for wear_name, _lo, _hi in WEAR_BUCKETS:
            if result.error is not None:
                break
            limiter.wait()
            try:
                response = _fetch_with_retry(market_hash_name(skin, wear_name))
            except SteamMarketAPIError as exc:
                result.error = str(exc)
                break
            finally:
                done_steps += 1
                if on_progress is not None:
                    on_progress(done_steps, total_steps)
            result.requests_made += 1

            price = _median_price(response)
            if price is None:
                result.wears_not_found += 1
                continue
            observations.append(
                signals.PriceObservationSignal(
                    source="steam_priceoverview",
                    wear_name=wear_name,
                    price=price,
                    currency="USD",
                    fetched_at=signals.now_utc(),
                    raw=response,
                )
            )

        signals.append_price_observations(skin.id, observations)
        result.observations += len(observations)
        pricing.recalculate_last_price(skin)
        result.skins_updated += 1

        if result.error is not None:
            break

    session.commit()
    contracts_module.resimulate(session, contract)

    return result
