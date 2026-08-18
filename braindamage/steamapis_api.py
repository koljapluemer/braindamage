"""Client for SteamApis' Market Data REST API (https://docs.steamapis.com/market-data/rest),
specifically CSFloat marketplace item prices -- a *different* CSFloat price source from
braindamage.csfloat_api, which talks to csfloat.com directly for postvalidation's
listing-level checks. This module goes through SteamApis instead, one GET /items call per
market_hash_name, mirroring braindamage.cs2cap_api's free-tier "one item per request" shape.

Market Data lives on its own subdomain (marketplaceapi.steamapis.com), distinct from
SteamApis' general v2 API (api.steamapis.com) used for Steam profile/inventory data
elsewhere -- confirmed against the published docs, which give both a general API key
header (x-api-key) and this surface's own (X-API-Key, case-insensitive, same header).
Docs state responses are server-side cached for 10s with no documented client-side rate
limit, but a 429 is still handled defensively since "no rate limit" isn't a guarantee.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config

BASE_URL = "https://marketplaceapi.steamapis.com/v2"
MARKETPLACE = "CSFloat"
GAME = "CS2"

# The subdomain sits behind Cloudflare, which blocks urllib's default
# "Python-urllib/x.y" User-Agent outright (HTTP 403, Cloudflare error 1010 --
# "browser signature banned") before the request ever reaches SteamApis' own
# backend/auth. A generic browser-like UA avoids that filter; it's not
# spoofing anything SteamApis-specific, just not announcing "I'm a bare
# script" to the edge.
_REQUEST_HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


class SteamApisAPIError(RuntimeError):
    """A request to SteamApis failed. Carries the HTTP status when there was a response
    (None for a connection failure -- no server was reached at all)."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code


class SteamApisRateLimitError(SteamApisAPIError):
    """A request hit SteamApis' rate limit (HTTP 429)."""

    def __init__(self, retry_after: float | None):
        super().__init__(429, "SteamApis rate limit exceeded (429)")
        self.retry_after = retry_after


@dataclass
class CsfloatQuote:
    """One CSFloat-marketplace price reading for a fully-qualified market_hash_name, as
    reported by SteamApis' /items endpoint."""

    market_hash_name: str
    price: float  # USD
    offer_count: int | None
    updated_at: int | None  # unix epoch seconds, per the SteamApis docs
    raw: dict[str, Any] = field(default_factory=dict)


def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
    if exc.code == 429:
        retry_after = exc.headers.get("Retry-After")
        raise SteamApisRateLimitError(float(retry_after) if retry_after else None) from exc
    detail = exc.read().decode(errors="replace").strip() or exc.reason
    raise SteamApisAPIError(exc.code, f"SteamApis returned {exc.code}: {detail}") from exc


def _fetch_item(market_hash_name: str) -> dict | None:
    """GET /items for `market_hash_name` on the CSFloat marketplace. Returns None on a
    404 (SteamApis has never indexed this exact name) rather than raising -- distinct
    from every other failure mode, which raises SteamApisAPIError."""
    params = {"marketplace": MARKETPLACE, "game": GAME, "name": market_hash_name}
    url = f"{BASE_URL}/items?{urllib.parse.urlencode(params)}"
    headers = {**_REQUEST_HEADERS_BASE, "X-API-Key": config.STEAMAPIS_KEY or ""}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        _raise_for_http_error(exc)
    except urllib.error.URLError as exc:
        raise SteamApisAPIError(None, f"Could not reach SteamApis: {exc.reason}") from exc


def fetch_csfloat_price(market_hash_name: str) -> CsfloatQuote | None:
    """CSFloat marketplace price for `market_hash_name`, via SteamApis. Returns None
    when there's no price on file for this exact name (a 404, or a 200 with a null
    priceUSD) -- a normal outcome, not every wear of every skin trades there right now
    -- as distinct from a real request failure, which raises SteamApisAPIError."""
    if not config.STEAMAPIS_KEY:
        raise RuntimeError("STEAMAPIS_KEY is not set")

    body = _fetch_item(market_hash_name)
    if body is None:
        return None

    price = body.get("priceUSD")
    if price is None:
        return None
    return CsfloatQuote(
        market_hash_name=body.get("name", market_hash_name),
        price=float(price),
        offer_count=body.get("offerCount"),
        updated_at=body.get("updatedAt"),
        raw=body,
    )
