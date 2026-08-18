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
from dataclasses import dataclass
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, mono_trade_overview, mono_trade_table, offer_combos, signals
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


@dataclass
class _Prepared:
    """Everything both handlers below need after validating one scrape
    payload and resolving it to a catalog Skin: the resolved skin, the raw
    market_hash_name (for BuyOrderSummarySignal's own market_hash_name
    field), and every offer converted to a SteamOfferSignal (price already
    normalized to USD). Not written to disk yet -- callers decide that."""

    skin: Skin
    market_hash_name: str
    entries: list[SteamOfferSignal]


def _validate_and_prepare(session: Session, payload: dict[str, Any]) -> _Prepared | dict[str, Any]:
    """Shared front half of both handlers below: validates the payload shape
    and currency, resolves market_hash_name to exactly one catalog Skin, and
    converts every offer to a SteamOfferSignal. Returns a `dict` (the
    {"ok": False, ...} error reply) on any expected failure -- callers must
    check `isinstance(result, dict)` before touching it as a _Prepared."""
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
    return _Prepared(skin=skin, market_hash_name=market_hash_name, entries=entries)


def _write_buy_order_summary(payload: dict[str, Any], prepared: _Prepared, currency: str) -> bool:
    """Writes payload's optional buy_order_summary (see handle_fetch_offers'
    docstring) as a BuyOrderSummarySignal -- returns whether one was written.
    Shared by both handlers so "Construct Contract" captures the same
    on-page data a plain scrape would, not less of it."""
    buy_order_summary = payload.get("buy_order_summary")
    if not buy_order_summary:
        return False
    bo_wear = buy_order_summary.get("wear_name")
    bo_price = buy_order_summary.get("price")
    bo_num_orders = buy_order_summary.get("num_orders")
    if not bo_wear or bo_price is None or bo_num_orders is None:
        return False
    if currency == "EUR":
        usd_bo_price = bo_price * config.EUR_USD_RATE
        bo_raw = {"original_currency": "EUR", "original_price": bo_price, "eur_usd_rate": config.EUR_USD_RATE}
    else:
        usd_bo_price = bo_price
        bo_raw = {}
    signals.append_buy_order_summaries(
        prepared.skin.id,
        [
            signals.BuyOrderSummarySignal(
                market_hash_name=prepared.market_hash_name,
                wear_name=bo_wear,
                price=usd_bo_price,
                currency="USD",
                num_orders=bo_num_orders,
                fetched_at=signals.now_utc(),
                raw=bo_raw,
            )
        ],
    )
    return True


def _recent_contract_history(skin_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """The `limit` most recently generated ContractHistorySignal entries for
    `skin_id`, newest first -- what the sidebar's contract history list
    (below everything else in the sidebar) renders."""
    entries = signals.read_contract_history(skin_id)[-limit:]
    return [
        {
            "generated_at": entry.generated_at.isoformat(),
            "expected_value": entry.expected_value,
            "raw_avg_float": entry.raw_avg_float,
        }
        for entry in reversed(entries)
    ]


def _serialize_combo(combo: offer_combos.ComboResult) -> dict[str, Any]:
    """JSON-safe rendering of one offer_combos.ComboResult for the sidebar's
    "Construct Contract" widget -- same fields braindamage.steam_offer_combos_report
    shows in its combo-card, just as data instead of HTML."""
    skin = combo.input_skin
    roi = combo.expected_value / combo.real_cost if combo.real_cost > 0 else None
    return {
        "skin_id": skin.id,
        "skin_name": skin.name,
        "collection_name": skin.collection_name,
        "rarity_name": skin.rarity_name,
        "stattrak": skin.stattrak,
        "avg_float": combo.avg_float,
        "raw_avg_float": combo.raw_avg_float,
        "real_cost": combo.real_cost,
        "expected_output_value": sum(o.contribution for o in combo.outcomes),
        "expected_value": combo.expected_value,
        "roi": roi,
        "offers": [
            {
                "wear_name": o.wear_name,
                "float_value": o.float_value,
                "pattern_seed": o.pattern_seed,
                "price": o.price,
            }
            for o in sorted(combo.offers, key=lambda o: o.price)
        ],
        "outcomes": [
            {
                "skin_name": o.skin_name,
                "collection_name": o.collection_name,
                "probability": o.probability,
                "predicted_wear": o.predicted_wear,
                "net_price": o.net_price,
                "contribution": o.contribution,
            }
            for o in combo.outcomes
        ],
    }


def handle_fetch_offers(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
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
    prepared = _validate_and_prepare(session, payload)
    if isinstance(prepared, dict):
        return prepared
    skin = prepared.skin

    signals.append_steam_offers(skin.id, prepared.entries)
    buy_order_written = _write_buy_order_summary(payload, prepared, payload["currency"])

    table = None
    table_error = None
    try:
        table = mono_trade_table.build_table(session, skin)
    except mono_trade_table.MonoTradeTableError as exc:
        table_error = str(exc)

    return {
        "ok": True,
        "skin_name": skin.name,
        "written": len(prepared.entries),
        "buy_order_written": buy_order_written,
        "table": table,
        "table_error": table_error,
        "contract_history": _recent_contract_history(skin.id),
    }


def handle_construct_contract(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """The "Construct Contract" sidebar button's handler: same payload shape
    as handle_fetch_offers (it's built from the exact same scrapePage()
    call), and writes to disk the same way -- but instead of the sidebar's
    always-on mono-trade table (which prices off whatever's on disk,
    however stale), this runs offer_combos.best_combos_for_skin directly
    against `payload`'s own offers and NOTHING ELSE, so the single combo it
    returns is only ever built from listings that are in the browser window
    at this exact moment -- i.e. actually buyable right now, not stitched
    together from offers that may have sold or been delisted since an
    earlier scrape.
    """
    prepared = _validate_and_prepare(session, payload)
    if isinstance(prepared, dict):
        return prepared
    skin = prepared.skin

    signals.append_steam_offers(skin.id, prepared.entries)
    buy_order_written = _write_buy_order_summary(payload, prepared, payload["currency"])

    in_window_offers = [o for o in prepared.entries if o.float_value is not None]
    if len(in_window_offers) < offer_combos.REQUIRED_INPUTS:
        return {
            "ok": False,
            "error": (
                f"Only {len(in_window_offers)} listing(s) with a visible float are on this page right now -- "
                f"need at least {offer_combos.REQUIRED_INPUTS} to construct a mono trade-up contract."
            ),
        }

    combos = offer_combos.best_combos_for_skin(session, skin, in_window_offers, top_n=1)
    if not combos:
        return {
            "ok": False,
            "error": (
                f"{skin.name} isn't a usable mono trade-up input right now -- wrong category, no next "
                "rarity tier, or its collection has no eligible output at that rarity."
            ),
        }
    combo = combos[0]

    signals.append_contract_history(
        skin.id,
        [
            signals.ContractHistorySignal(
                expected_value=combo.expected_value,
                raw_avg_float=combo.raw_avg_float,
                generated_at=signals.now_utc(),
            )
        ],
    )

    return {
        "ok": True,
        "skin_name": skin.name,
        "written": len(prepared.entries),
        "buy_order_written": buy_order_written,
        "contract": _serialize_combo(combo),
        "contract_history": _recent_contract_history(skin.id),
    }


def handle_overview_chunk(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Build one independently measurable rarity/StatTrak overview batch."""
    rarity = payload.get("rarity_name")
    stattrak = payload.get("stattrak")
    if rarity not in mono_trade_overview.tradeup.INPUT_RARITIES or not isinstance(stattrak, bool):
        return {"ok": False, "error": "Invalid overview batch"}
    return mono_trade_overview.build_overview(
        session, rarities=[rarity], stattrak_values=[stattrak]
    )


# Action names the sidebar's payload carries in its "action" field --
# "fetch_offers" (the default, for payloads with none, e.g. from before this
# field existed) is the always-on scrape+table refresh; "construct_contract"
# is the "Construct Contract" button. Both share the exact same payload
# shape and disk-write behavior, differing only in what they compute after.
_HANDLERS = {
    "fetch_offers": handle_fetch_offers,
    "construct_contract": handle_construct_contract,
    "overview": lambda session, _payload: mono_trade_overview.build_overview(session),
    "overview_chunk": handle_overview_chunk,
}


def handle_message(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatches one native-messaging request to its handler by
    payload["action"] (see _HANDLERS) -- never raises for an expected
    failure mode, always returns {"ok": False, "error": ...} instead."""
    action = payload.get("action", "fetch_offers") if isinstance(payload, dict) else "fetch_offers"
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"ok": False, "error": f"Unknown action {action!r}"}
    return handler(session, payload)


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
