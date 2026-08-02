"""Client for the CS2Cap prices API (https://docs.cs2cap.com/api-reference/prices).

Uses GET /prices (one item per request), not POST /prices/batch — batch lookup requires
a Starter+ subscription; GET /prices is the endpoint available on the Free tier.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal
from .models import MarketItem, MarketItemExternalId, PriceObservation, Skin

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
    # market items for which CS2Cap returned no price data at all
    items_not_found: int
    # Set if a request failed partway through (e.g. rate limit). Whatever was fetched
    # before the failure is still committed — this just explains why the run stopped early.
    error: str | None = None


def select_market_items(
    session: Session, collection_id: str | None, variant: str | None
) -> list[MarketItem]:
    query = select(MarketItem).join(Skin, MarketItem.skin_id == Skin.id)
    if collection_id:
        query = query.where(Skin.collection_id == collection_id)
    if variant == "normal":
        query = query.where(MarketItem.stattrak.is_(False), MarketItem.souvenir.is_(False))
    elif variant == "stattrak":
        query = query.where(MarketItem.stattrak.is_(True))
    elif variant == "souvenir":
        query = query.where(MarketItem.souvenir.is_(True))
    return list(session.scalars(query).all())


def _fetch_prices(market_hash_name: str, phase: str | None, currency: str) -> dict:
    params = {"market_hash_name": market_hash_name, "currency": currency}
    if phase:
        params["phase"] = phase
    url = f"{BASE_URL}/prices?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.CS2CAP_API_KEY}"}
    )
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
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _upsert_external_id(session: Session, market_item: MarketItem, external_id: object) -> None:
    if external_id is None:
        return
    external_id = str(external_id)
    existing = session.scalar(
        select(MarketItemExternalId).where(
            MarketItemExternalId.market_item_id == market_item.id,
            MarketItemExternalId.source == "cs2cap",
        )
    )
    if existing is None:
        session.add(
            MarketItemExternalId(market_item_id=market_item.id, source="cs2cap", external_id=external_id)
        )
    else:
        existing.external_id = external_id


def run_price_import(
    collection_id: str | None = None,
    variant: str | None = None,
    currency: str = "USD",
) -> PriceImportResult:
    if not config.CS2CAP_API_KEY:
        raise RuntimeError("CS2CAP_API_KEY is not set")

    requests_made = 0
    observations = 0
    items_not_found = 0
    error: str | None = None

    with SessionLocal() as session:
        market_items = select_market_items(session, collection_id, variant)

        for market_item in market_items:
            try:
                response = _fetch_prices(market_item.market_hash_name, market_item.phase, currency)
            except Cs2capAPIError as exc:
                error = str(exc)
                break
            requests_made += 1

            quotes = response.get("items") or []
            if not quotes:
                items_not_found += 1
                continue

            for quote in quotes:
                price = _quote_price(quote)
                if price is None:
                    continue
                session.add(
                    PriceObservation(
                        market_item_id=market_item.id,
                        source="cs2cap",
                        provider=quote.get("provider"),
                        side="ask",
                        currency=currency,
                        price=price,
                        quantity=quote.get("quantity"),
                        observed_at=_parse_timestamp(quote.get("timestamp")),
                        raw=quote,
                    )
                )
                observations += 1
                _upsert_external_id(session, market_item, quote.get("item_id"))
            session.commit()

    return PriceImportResult(
        requests_made=requests_made,
        observations=observations,
        items_not_found=items_not_found,
        error=error,
    )
