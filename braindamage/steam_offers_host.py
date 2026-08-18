"""Firefox native-messaging host for the companion webext/ extension: receives
one scraped Steam Community Market listing page (float/pattern/price per
listing, plus the page's market_hash_name and detected wallet currency) and
writes it to disk as braindamage.signals.SteamOfferSignal entries, exactly
the write path braindamage.steam_offer_combos later reads from.

Spawned fresh per browser.runtime.sendNativeMessage() call (Firefox's native
messaging model) -- reads exactly one length-prefixed JSON message from
stdin, handles it, writes exactly one reply to stdout, and exits. No
persistent loop: a click-to-fetch button is inherently one request/response,
so there's nothing a long-lived process would buy here.

handle_message() is the actual logic, kept separate from the stdio framing
so it's unit-testable without spawning a subprocess (see
tests/test_steam_offers_host.py).
"""

from __future__ import annotations

import json
import struct
import sys
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, mono_trade_table, signals
from .db import SessionLocal, upgrade_database
from .market_names import parse_market_hash_name
from .models import Skin
from .signals import SteamOfferSignal

# Currencies this host can turn into USD -- USD passes through unchanged;
# EUR is converted at write time using config.EUR_USD_RATE (see there for
# why that's a hand-maintained rate rather than a live-fetched one). Any
# other currency is rejected outright: this app assumes USD everywhere
# downstream (pricing, EV math), so silently accepting an unconvertible
# currency would corrupt that math with no error.
_SUPPORTED_CURRENCIES = ("USD", "EUR")


def handle_message(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Validates and writes one scrape payload; never raises for an expected
    failure mode -- always returns {"ok": False, "error": ...} instead, so
    the stdio loop only ever needs to JSON-dump whatever this returns.

    Expected payload shape:
        {"market_hash_name": str, "currency": str,
         "offers": [{"wear_name": str | None, "float_value": float,
                     "pattern_seed": int | None, "price": float}, ...],
         "buy_order_summary": {"wear_name": str, "price": float,
                                "num_orders": int} | None}

    market_hash_name only needs to be ONE representative "<name> (<wear>)"
    string (used to resolve the Skin) -- a single Steam Market page can list
    every wear condition of one weapon together, so offers may span more
    than one wear. Each offer's own "wear_name" (if present) is used for
    that offer's SteamOfferSignal; market_hash_name's parsed wear is only a
    fallback for offers that didn't carry one.

    buy_order_summary is optional (Steam only renders that line once a wear
    filter is active on the page -- see webext/sidebar.js) and shares the
    top-level `currency`, since it's scraped off the same page as everything
    else here. When present it's written as a BuyOrderSummarySignal.

    On success, the reply also carries the sidebar's mono-trade price table
    for the resolved skin (see braindamage.mono_trade_table) -- "table" is
    None with "table_error" explaining why if the skin isn't a usable
    trade-up input, e.g. a Covert (no next rarity) or an orphaned collection.
    """
    try:
        market_hash_name = payload["market_hash_name"]
        currency = payload["currency"]
        offers = payload["offers"]
    except KeyError as exc:
        return {"ok": False, "error": f"Malformed payload: missing {exc}"}
    if not isinstance(market_hash_name, str) or not isinstance(offers, list):
        return {"ok": False, "error": "Malformed payload: market_hash_name must be a string, offers a list"}

    if currency not in _SUPPORTED_CURRENCIES:
        return {
            "ok": False,
            "error": (
                f"Steam account currency is {currency!r} -- only {'/'.join(_SUPPORTED_CURRENCIES)} are "
                "supported. Set your Steam account region/currency to one of those and re-scrape."
            ),
        }
    if currency == "EUR" and config.EUR_USD_RATE is None:
        return {
            "ok": False,
            "error": "Currency is EUR but EUR_USD_RATE isn't set in .env -- see .env.example.",
        }

    base_name, wear_name, stattrak, souvenir = parse_market_hash_name(market_hash_name)
    if wear_name is None:
        return {"ok": False, "error": f"Could not find a known wear suffix in {market_hash_name!r}"}

    query = select(Skin).where(
        Skin.name == base_name, Skin.stattrak == stattrak, Skin.souvenir == souvenir
    )
    matches = list(session.scalars(query).all())
    if not matches:
        return {"ok": False, "error": f"No matching skin in the catalog for {base_name!r}"}
    if len(matches) > 1:
        return {
            "ok": False,
            "error": (
                f"Ambiguous: {len(matches)} skins match {base_name!r} (likely a Doppler/Gamma "
                "Doppler phase collision) -- can't disambiguate phase from Steam's listing name alone."
            ),
        }
    skin = matches[0]

    entries = []
    for offer in offers:
        original_price = offer["price"]
        if currency == "EUR":
            usd_price = original_price * config.EUR_USD_RATE
            raw = {"original_currency": "EUR", "original_price": original_price, "eur_usd_rate": config.EUR_USD_RATE}
        else:
            usd_price = original_price
            raw = {}
        entries.append(
            SteamOfferSignal(
                market_hash_name=market_hash_name,
                wear_name=offer.get("wear_name") or wear_name,
                float_value=offer.get("float_value"),
                pattern_seed=offer.get("pattern_seed"),
                price=usd_price,
                currency="USD",
                fetched_at=signals.now_utc(),
                raw=raw,
            )
        )
    signals.append_steam_offers(skin.id, entries)

    buy_order_written = False
    buy_order_summary = payload.get("buy_order_summary")
    if buy_order_summary:
        bo_wear = buy_order_summary.get("wear_name")
        bo_price = buy_order_summary.get("price")
        bo_num_orders = buy_order_summary.get("num_orders")
        if bo_wear and bo_price is not None and bo_num_orders is not None:
            if currency == "EUR":
                usd_bo_price = bo_price * config.EUR_USD_RATE
                bo_raw = {"original_currency": "EUR", "original_price": bo_price, "eur_usd_rate": config.EUR_USD_RATE}
            else:
                usd_bo_price = bo_price
                bo_raw = {}
            signals.append_buy_order_summaries(
                skin.id,
                [
                    signals.BuyOrderSummarySignal(
                        market_hash_name=market_hash_name,
                        wear_name=bo_wear,
                        price=usd_bo_price,
                        currency="USD",
                        num_orders=bo_num_orders,
                        fetched_at=signals.now_utc(),
                        raw=bo_raw,
                    )
                ],
            )
            buy_order_written = True

    table = None
    table_error = None
    try:
        table = mono_trade_table.build_table(session, skin)
    except mono_trade_table.MonoTradeTableError as exc:
        table_error = str(exc)

    return {
        "ok": True,
        "skin_name": skin.name,
        "written": len(entries),
        "buy_order_written": buy_order_written,
        "table": table,
        "table_error": table_error,
    }


# --- Native messaging stdio framing ---------------------------------------------

# Firefox's native messaging protocol: each message is a 4-byte native-byte-
# order length prefix followed by that many bytes of UTF-8 JSON, in both
# directions. (Size limits: 4GB extension->host, 1MB host->extension --
# irrelevant here, a scrape payload and an ack are both tiny.)
_LENGTH_STRUCT = struct.Struct("@I")


def _read_message(stream: BinaryIO) -> dict[str, Any] | None:
    raw_length = stream.read(4)
    if len(raw_length) < 4:
        return None  # stdin closed -- nothing more to read
    (length,) = _LENGTH_STRUCT.unpack(raw_length)
    return json.loads(stream.read(length).decode("utf-8"))


def _write_message(stream: BinaryIO, obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    stream.write(_LENGTH_STRUCT.pack(len(body)))
    stream.write(body)
    stream.flush()


def main() -> int:
    message = _read_message(sys.stdin.buffer)
    if message is None:
        return 0

    upgrade_database()
    with SessionLocal() as session:
        try:
            reply = handle_message(session, message)
        except Exception as exc:  # noqa: BLE001 -- last line of defense: always reply, never crash silently
            reply = {"ok": False, "error": f"Unexpected error: {exc}"}

    _write_message(sys.stdout.buffer, reply)
    return 0
