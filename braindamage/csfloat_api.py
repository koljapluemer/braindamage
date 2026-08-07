"""Client for the CSFloat public API (https://docs.csfloat.com).

Unlike CS2Cap (braindamage.cs2cap_api) or Steam's own priceoverview
(braindamage.steam_market_api) -- both aggregate wear-tier-only prices --
CSFloat exposes individual live floated listings on its own marketplace, so
this is used specifically for postvalidation (braindamage.postvalidate):
confirming (or refuting) a wear-tier price approximation against real,
currently-buyable offers at specific floats.

Free with a CSFloat account (config.CSFLOAT_API_KEY) -- no documented tier or
cost. CSFloat also documents no rate limit, so the real ceiling is unknown
and *will* be hit on any batch run of meaningful size (confirmed in
practice). Requests are self-paced (_RateLimiter) starting from a
conservative guess, but a 429 permanently slows that pace down for every
subsequent call in the process (_RateLimiter.slow_down), not just the one
that got rate-limited -- a fixed retry-then-give-up on one call is not enough
when the whole run is exceeding the limit, not just one unlucky request.

CSFloat has no buy-order-book endpoint (confirmed against the full published
reference: Introduction / Authentication / Listings only) -- so unlike
CS2Cap's bids, the sell side here is the live *lowest ask* per wear bucket,
not a buy order.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config

BASE_URL = "https://csfloat.com/api/v1"

REQUEST_INTERVAL_SECONDS = 1.5
_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0, 90.0, 180.0, 300.0)
_MAX_RATE_LIMIT_RETRIES = 7
# Ceiling for _RateLimiter.slow_down() -- past this, a persistently rate-limited
# run should surface as a real error (see postvalidate.py's per-contract error
# handling) rather than silently crawl forever.
_MAX_STEADY_INTERVAL_SECONDS = 20.0
# Ceiling for any single backoff sleep, INCLUDING one CSFloat's own
# Retry-After header suggests -- a server-supplied wait is a hint, never
# trusted unclamped, since there's no telling how large it could be.
_MAX_BACKOFF_SECONDS = 300.0


class CsfloatAPIError(RuntimeError):
    """A request to CSFloat failed. Carries the HTTP status when there was a response."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


class CsfloatRateLimitError(CsfloatAPIError):
    """A request hit CSFloat's (undocumented) rate limit (HTTP 429)."""

    def __init__(self, retry_after: float | None):
        super().__init__(429, "CSFloat API rate limit exceeded (429)")
        self.retry_after = retry_after


class CsfloatMaxBackoffExceeded(CsfloatRateLimitError):
    """A 429's backoff would have to be at or past `_MAX_BACKOFF_SECONDS` to
    respect it -- in practice this means CSFloat isn't briefly unhappy, it's
    the kind of block that lasts hours, so retrying (this call, the rest of
    this contract's ranges, or the rest of the batch -- see
    postvalidate.postvalidate_contracts) is pointless rather than just slow.
    A subclass of CsfloatRateLimitError so existing `except CsfloatAPIError`/
    `except CsfloatRateLimitError` handling still catches it unchanged."""


@dataclass
class FloatListing:
    """One individual, currently-buyable listing on CSFloat."""

    listing_id: str
    market_hash_name: str
    wear_name: str | None
    float_value: float | None
    price: float  # USD
    listing_type: str
    raw: dict[str, Any] = field(default_factory=dict)


class _RateLimiter:
    """Sleeps as needed so consecutive wait() calls are never closer together
    than `min_interval` seconds (steam_market_api._RateLimiter's approach),
    plus an adaptive slow_down() -- since CSFloat documents no actual limit,
    the starting interval is a guess, and a 429 means the guess was wrong for
    *this run*, not just this one request. Every subsequent wait() across the
    whole process backs off further once that happens."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            remaining = self._min_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()

    def slow_down(self) -> None:
        self._min_interval = min(self._min_interval * 2, _MAX_STEADY_INTERVAL_SECONDS)


_limiter = _RateLimiter(REQUEST_INTERVAL_SECONDS)


def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
    if exc.code == 429:
        retry_after = exc.headers.get("Retry-After")
        raise CsfloatRateLimitError(float(retry_after) if retry_after else None) from exc
    detail = exc.read().decode(errors="replace").strip() or exc.reason
    raise CsfloatAPIError(exc.code, f"CSFloat API returned {exc.code}: {detail}") from exc


def _fetch_listings(params: dict[str, Any]) -> list[dict]:
    """GET /listings -- the published docs claim a bare JSON array response,
    but that's out of date: confirmed live (2026-08-06) to actually be
    `{"data": [...], ...}`. Accepts a bare array too in case that ever
    reverts, and fails loudly with the real body on anything else rather than
    guessing at a shape again."""
    url = f"{BASE_URL}/listings?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": config.CSFLOAT_API_KEY or ""})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        _raise_for_http_error(exc)
    except urllib.error.URLError as exc:
        raise CsfloatAPIError(None, f"Could not reach CSFloat API: {exc.reason}") from exc

    body = json.loads(raw)
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    preview = raw.decode(errors="replace")[:500]
    raise CsfloatAPIError(
        None, f"CSFloat /listings returned 200 but the body wasn't a recognized shape: {preview}"
    )


def _fetch_listings_with_retry(params: dict[str, Any], max_retries: int = _MAX_RATE_LIMIT_RETRIES) -> list[dict]:
    for attempt in range(max_retries):
        _limiter.wait()
        try:
            return _fetch_listings(params)
        except CsfloatRateLimitError as exc:
            _limiter.slow_down()
            uncapped = exc.retry_after or _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            if uncapped >= _MAX_BACKOFF_SECONDS:
                # Don't bother sleeping out a backoff this long just to retry --
                # a wait this size means CSFloat is sustained-unhappy, not
                # flaky, and won't recover within this run's lifetime.
                print(
                    f"CSFloat rate limit hit (attempt {attempt + 1}/{max_retries}) -- backoff would be "
                    f"{uncapped:.0f}s, at or past the {_MAX_BACKOFF_SECONDS:.0f}s ceiling; treating this "
                    "as a sustained block rather than retrying.",
                    file=sys.stderr,
                )
                raise CsfloatMaxBackoffExceeded(exc.retry_after) from exc
            if attempt == max_retries - 1:
                raise
            backoff = min(uncapped, _MAX_BACKOFF_SECONDS)
            # A sleep this long must never look like a hang -- always visible,
            # never just silently eaten inside a coarser per-contract progress bar.
            print(
                f"CSFloat rate limit hit (attempt {attempt + 1}/{max_retries}) -- "
                f"backing off {backoff:.0f}s before retrying "
                f"(steady pace now {_limiter._min_interval:.1f}s/request)",
                file=sys.stderr,
            )
            time.sleep(backoff)
    raise AssertionError("unreachable")  # loop always returns or raises


def _parse_listing(item: dict) -> FloatListing:
    inner = item.get("item") or {}
    return FloatListing(
        listing_id=str(item.get("id")),
        market_hash_name=inner.get("market_hash_name", ""),
        wear_name=inner.get("wear_name"),
        float_value=inner.get("float_value"),
        price=item["price"] / 100,
        listing_type=item.get("type", "buy_now"),
        raw=item,
    )


def cheapest_listings_in_float_range(
    market_hash_name: str, min_float: float, max_float: float, limit: int = 10
) -> list[FloatListing]:
    """Up to `limit` currently-buyable (`type=buy_now`) listings of
    `market_hash_name` whose float falls in [min_float, max_float], cheapest
    first -- one call, since `limit` never exceeds CSFloat's own per-request
    cap of 50."""
    params = {
        "market_hash_name": market_hash_name,
        "min_float": f"{min_float:.6f}",
        "max_float": f"{max_float:.6f}",
        "type": "buy_now",
        "sort_by": "lowest_price",
        "limit": limit,
    }
    return [_parse_listing(item) for item in _fetch_listings_with_retry(params)]


def lowest_ask(market_hash_name: str) -> float | None:
    """The single cheapest currently-buyable listing's price for
    `market_hash_name` (wear is already baked into the name), or None if
    nothing is listed for sale right now."""
    params = {
        "market_hash_name": market_hash_name,
        "type": "buy_now",
        "sort_by": "lowest_price",
        "limit": 1,
    }
    items = _fetch_listings_with_retry(params)
    return items[0]["price"] / 100 if items else None
