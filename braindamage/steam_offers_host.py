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
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, mono_trade_overview, mono_trade_table, offer_combos, signals, skins_overview
from .db import SessionLocal, upgrade_database
from .market_names import market_hash_name, parse_market_hash_name
from .models import Skin
from .signals import SteamOfferSignal

# Currencies this host can turn into USD -- USD passes through unchanged;
# EUR is converted at write time using config.EUR_USD_RATE (see there for
# why that's a hand-maintained rate rather than a live-fetched one). Any
# other currency is rejected outright: this app assumes USD everywhere
# downstream (pricing, EV math), so silently accepting an unconvertible
# currency would corrupt that math with no error.
_SUPPORTED_CURRENCIES = ("USD", "EUR")


def _validate_currency(currency: str) -> dict[str, Any] | None:
    """Shared front half of both _validate_and_prepare (Steam) and
    _validate_and_prepare_csfloat: {"ok": False, ...} if `currency` isn't
    usable, else None."""
    if currency not in _SUPPORTED_CURRENCIES:
        return {
            "ok": False,
            "error": (
                f"Account currency is {currency!r} -- only {'/'.join(_SUPPORTED_CURRENCIES)} are "
                "supported. Set your account region/currency to one of those and re-scrape."
            ),
        }
    if currency == "EUR" and config.EUR_USD_RATE is None:
        return {
            "ok": False,
            "error": "Currency is EUR but EUR_USD_RATE isn't set in .env -- see .env.example.",
        }
    return None


def _to_usd(price: float, currency: str) -> tuple[float, dict[str, Any]]:
    """Converts one scraped price to USD, returning (usd_price, raw) where
    `raw` records the original currency/rate when a conversion happened (see
    SteamOfferSignal.raw / MarketOfferSignal.raw) -- empty for USD, since
    there's nothing to record. Caller must have already validated `currency`
    via _validate_currency."""
    if currency == "EUR":
        return price * config.EUR_USD_RATE, {
            "original_currency": "EUR",
            "original_price": price,
            "eur_usd_rate": config.EUR_USD_RATE,
        }
    return price, {}


def _resolved_input_source(payload: dict[str, Any]) -> str:
    """payload["input_source"] (the sidebar's market dropdown -- see
    mono_trade_table.INPUT_SOURCES) if it's a recognized value, else the
    default -- never lets a stale/out-of-sync extension send something this
    host can't handle through to build_table/build_float_diagram_data."""
    source = payload.get("input_source")
    return source if source in mono_trade_table.INPUT_SOURCES else mono_trade_table.DEFAULT_INPUT_SOURCE


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

    currency_error = _validate_currency(currency)
    if currency_error is not None:
        return currency_error

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

    # One shared timestamp for every offer in this scrape, not signals.now_utc()
    # called per-offer -- mono_trade_table groups offers into "batches" (one
    # page scrape) by exact fetched_at equality, so entries from the same
    # scrape must carry an identical timestamp.
    fetched_at = signals.now_utc()
    comprehensive = bool(payload.get("comprehensive"))
    entries = []
    for offer in offers:
        usd_price, raw = _to_usd(offer["price"], currency)
        entries.append(
            SteamOfferSignal(
                market_hash_name=market_hash_name,
                wear_name=offer.get("wear_name") or wear_name,
                float_value=offer.get("float_value"),
                pattern_seed=offer.get("pattern_seed"),
                price=usd_price,
                currency="USD",
                fetched_at=fetched_at,
                comprehensive=comprehensive,
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
    usd_bo_price, bo_raw = _to_usd(bo_price, currency)
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


def _offer_pattern_seed(offer: Any) -> int | None:
    """pattern_seed for one combo offer -- SteamOfferSignal carries it as a
    real field; MarketOfferSignal (CSFloat) has no such field at all, so
    _validate_and_prepare_csfloat stashes it inside `raw` instead. Tolerant
    of either shape so _serialize_combo works for combos built from both."""
    seed = getattr(offer, "pattern_seed", None)
    if seed is not None:
        return seed
    raw = getattr(offer, "raw", None)
    return raw.get("pattern_seed") if isinstance(raw, dict) else None


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
                "pattern_seed": _offer_pattern_seed(o),
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
                                "num_orders": int} | None,
         "input_source": "steam" | "csfloat" | None,
         "comprehensive": bool | None}

    comprehensive (default False) is set by the sidebar's "Auto-Scroll &
    Save" flow, which scrolls the listing page to load as many offers as
    Steam will render (up to 1000) before calling this -- it's stamped onto
    every SteamOfferSignal written here (see that class) so a near-complete
    snapshot can be told apart from an ordinary single-page scrape later.

    input_source (default "steam" -- see mono_trade_table.INPUT_SOURCES)
    only controls which on-disk offer signal the *returned* "table"/
    "float_diagrams" are priced from -- it has no effect on what gets
    written: this scrape's own offers are always saved as SteamOfferSignal,
    regardless of the sidebar's market dropdown.

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
    "float_diagrams" carries the sidebar's float-vs-price/revenue/EV chart
    data (braindamage.mono_trade_table.build_float_diagram_data) -- None
    under the same condition as "table" (they share the same validity
    check), no separate error message since table_error already covers it.
    """
    prepared = _validate_and_prepare(session, payload)
    if isinstance(prepared, dict):
        return prepared
    skin = prepared.skin

    signals.append_steam_offers(skin.id, prepared.entries)
    buy_order_written = _write_buy_order_summary(payload, prepared, payload["currency"])
    input_source = _resolved_input_source(payload)

    table = None
    table_error = None
    float_diagrams = None
    try:
        table = mono_trade_table.build_table(session, skin, input_source=input_source)
        float_diagrams = mono_trade_table.build_float_diagram_data(session, skin, input_source=input_source)
    except mono_trade_table.MonoTradeTableError as exc:
        table_error = str(exc)

    return {
        "ok": True,
        "skin_name": skin.name,
        "written": len(prepared.entries),
        "buy_order_written": buy_order_written,
        "table": table,
        "table_error": table_error,
        "float_diagrams": float_diagrams,
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


@dataclass
class _CsfloatGroup:
    """One (stattrak, souvenir) group of offers scraped from a single CSFloat
    search page, resolved to its catalog Skin -- see
    _validate_and_prepare_csfloat's docstring for why a page can span more
    than one group at once."""

    skin: Skin
    entries: list[signals.MarketOfferSignal]


def _validate_and_prepare_csfloat(
    session: Session, payload: dict[str, Any]
) -> tuple[list[_CsfloatGroup], list[str]] | dict[str, Any]:
    """CSFloat counterpart to _validate_and_prepare: validates the payload
    shape and currency, then resolves every offer to a catalog Skin and
    converts it to a MarketOfferSignal. Returns a `dict` (the {"ok": False,
    ...} error reply) on a payload-level failure -- callers must check
    `isinstance(result, dict)` before touching it as the (groups, errors)
    tuple.

    Unlike a Steam listing page (one market_hash_name, so exactly one Skin),
    a CSFloat search page scoped to one weapon+paint can mix StatTrak/
    Souvenir/normal listings together (see webext/sidebar.js's CSFloat
    scraper) -- each is a genuinely different catalog Skin, so offers are
    grouped by (stattrak, souvenir) and each group resolved independently.
    A group that fails to resolve (no match, or an ambiguous Doppler-phase
    collision) is dropped with its own message in the returned error list
    rather than failing the whole request -- the other groups still get
    saved.
    """
    try:
        base_skin_name = payload["base_skin_name"]
        currency = payload["currency"]
        offers = payload["offers"]
    except KeyError as exc:
        return {"ok": False, "error": f"Malformed payload: missing {exc}"}
    if not isinstance(base_skin_name, str) or not isinstance(offers, list):
        return {"ok": False, "error": "Malformed payload: base_skin_name must be a string, offers a list"}

    currency_error = _validate_currency(currency)
    if currency_error is not None:
        return currency_error

    offers_by_variant: dict[tuple[bool, bool], list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        offers_by_variant[(bool(offer.get("stattrak")), bool(offer.get("souvenir")))].append(offer)

    # One shared timestamp for every offer in this scrape -- see
    # _validate_and_prepare's identical rationale (mono_trade_table groups
    # offers into "batches" by exact fetched_at equality).
    fetched_at = signals.now_utc()
    comprehensive = bool(payload.get("comprehensive"))
    groups: list[_CsfloatGroup] = []
    errors: list[str] = []

    for (stattrak, souvenir), variant_offers in offers_by_variant.items():
        query = select(Skin).where(
            Skin.name == base_skin_name, Skin.stattrak == stattrak, Skin.souvenir == souvenir
        )
        matches = list(session.scalars(query).all())
        label = f"{'StatTrak™ ' if stattrak else 'Souvenir ' if souvenir else ''}{base_skin_name}"
        if not matches:
            errors.append(f"No matching skin in the catalog for {label!r}")
            continue
        if len(matches) > 1:
            errors.append(
                f"Ambiguous: {len(matches)} skins match {label!r} (likely a Doppler/Gamma Doppler "
                "phase collision) -- can't disambiguate phase from CSFloat's listing name alone."
            )
            continue
        skin = matches[0]

        entries = []
        for offer in variant_offers:
            usd_price, raw = _to_usd(offer["price"], currency)
            raw["pattern_seed"] = offer.get("pattern_seed")
            wear_name = offer.get("wear_name")
            entries.append(
                signals.MarketOfferSignal(
                    source="csfloat",
                    listing_id=str(offer["listing_id"]),
                    market_hash_name=market_hash_name(skin, wear_name) if wear_name else base_skin_name,
                    wear_name=wear_name,
                    float_value=offer.get("float_value"),
                    price=usd_price,
                    currency="USD",
                    listing_type=offer.get("listing_type") or "buy_now",
                    fetched_at=fetched_at,
                    comprehensive=comprehensive,
                    raw=raw,
                )
            )
        groups.append(_CsfloatGroup(skin=skin, entries=entries))

    return groups, errors


def handle_fetch_csfloat_offers(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Validates and writes one CSFloat search-page scrape (webext/sidebar.js,
    the CSfloat counterpart to handle_fetch_offers); never raises for an
    expected failure mode -- always returns {"ok": False, ...} instead.

    Expected payload shape:
        {"base_skin_name": str, "currency": str,
         "offers": [{"wear_name": str | None, "float_value": float | None,
                     "pattern_seed": int | None, "price": float,
                     "stattrak": bool, "souvenir": bool,
                     "listing_id": str, "listing_type": str}, ...],
         "input_source": "steam" | "csfloat" | None,
         "comprehensive": bool | None}

    comprehensive (default False) is set by the sidebar's "Auto-Scroll &
    Save" flow -- see SteamOfferSignal.comprehensive / MarketOfferSignal.comprehensive
    for what it means on each side; stamped onto every MarketOfferSignal
    written here the same way handle_fetch_offers stamps it onto
    SteamOfferSignal.

    base_skin_name has no wear/StatTrak/Souvenir baked in (unlike Steam's
    market_hash_name) -- CSFloat's item-name element never renders those
    (see webext/sidebar.js's CSFloat scraper), so each offer carries its own
    stattrak/souvenir flags instead, and offers are grouped by that pair
    before being resolved to a Skin (see _validate_and_prepare_csfloat).

    On success, "table"/"float_diagrams" are built (see
    mono_trade_table.build_table/build_float_diagram_data, priced from
    `input_source`, default "steam") for whichever resolved group had the
    most offers -- a page mixing e.g. StatTrak and normal listings still
    needs exactly one skin to show the sidebar's table for. "group_errors"
    carries a message for every (stattrak, souvenir) group that failed to
    resolve, if any -- the groups that DID resolve are still saved.
    """
    prepared = _validate_and_prepare_csfloat(session, payload)
    if isinstance(prepared, dict):
        return prepared
    groups, errors = prepared
    if not groups:
        return {"ok": False, "error": "; ".join(errors) or "No offers to save"}

    total_written = 0
    for group in groups:
        signals.append_market_offers(group.skin.id, group.entries)
        total_written += len(group.entries)

    primary = max(groups, key=lambda g: len(g.entries))
    input_source = _resolved_input_source(payload)

    table = None
    table_error = None
    float_diagrams = None
    try:
        table = mono_trade_table.build_table(session, primary.skin, input_source=input_source)
        float_diagrams = mono_trade_table.build_float_diagram_data(
            session, primary.skin, input_source=input_source
        )
    except mono_trade_table.MonoTradeTableError as exc:
        table_error = str(exc)

    return {
        "ok": True,
        "skin_name": primary.skin.name,
        "written": total_written,
        "group_errors": errors,
        "table": table,
        "table_error": table_error,
        "float_diagrams": float_diagrams,
    }


def handle_construct_contract_csfloat(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """The CSfloat "Construct Contract" button's handler -- CSfloat
    counterpart to handle_construct_contract: same payload shape as
    handle_fetch_csfloat_offers (built from the same scrapeCsfloatPage()
    call), writes to disk the same way, but builds the contract from
    exactly what's in the browser window right now -- buy_now listings
    only, same exclusion as everywhere else CSFloat input pricing is read
    (see _validate_and_prepare_csfloat's docstring on why auctions don't
    count) -- not whatever's already on disk.

    Output pricing is untouched by any of this: offer_combos.best_combos_for_skin
    always prices outcomes from braindamage.pricing's existing buy-order-
    summary/last-price machinery (populated by Steam scrapes in this app, in
    practice -- CSFloat has no buy-order-book endpoint at all, see
    csfloat_api's module docstring), regardless of where the *input* offers
    came from. This only ever replaces the *input* side with CSFloat's own
    live listings -- outcome/trade prices stay exactly as they already were.

    A page can resolve to more than one skin (StatTrak/Souvenir/normal mixed
    -- see _validate_and_prepare_csfloat); each resolved group with enough
    in-window offers gets its own best combo, and the single highest-EV one
    across all of them wins (same "pool everything, rank globally" approach
    as braindamage.mono_offer_combos.find_best_combos).
    """
    prepared = _validate_and_prepare_csfloat(session, payload)
    if isinstance(prepared, dict):
        return prepared
    groups, errors = prepared
    if not groups:
        return {"ok": False, "error": "; ".join(errors) or "No offers to save"}

    total_written = 0
    for group in groups:
        signals.append_market_offers(group.skin.id, group.entries)
        total_written += len(group.entries)

    best_combo: offer_combos.ComboResult | None = None
    largest_window = 0
    for group in groups:
        in_window_offers = [
            o for o in group.entries if o.float_value is not None and o.listing_type == "buy_now"
        ]
        largest_window = max(largest_window, len(in_window_offers))
        if len(in_window_offers) < offer_combos.REQUIRED_INPUTS:
            continue
        combos = offer_combos.best_combos_for_skin(session, group.skin, in_window_offers, top_n=1)
        if combos and (best_combo is None or combos[0].expected_value > best_combo.expected_value):
            best_combo = combos[0]

    if best_combo is None:
        if largest_window < offer_combos.REQUIRED_INPUTS:
            return {
                "ok": False,
                "error": (
                    f"Only {largest_window} buy-now listing(s) with a visible float are on this page right "
                    f"now -- need at least {offer_combos.REQUIRED_INPUTS} to construct a mono trade-up contract."
                ),
            }
        return {
            "ok": False,
            "error": (
                "None of the skin(s) on this page are usable mono trade-up inputs right now -- wrong "
                "category, no next rarity tier, or their collection has no eligible output at that rarity."
            ),
        }

    signals.append_contract_history(
        best_combo.input_skin.id,
        [
            signals.ContractHistorySignal(
                expected_value=best_combo.expected_value,
                raw_avg_float=best_combo.raw_avg_float,
                generated_at=signals.now_utc(),
            )
        ],
    )

    return {
        "ok": True,
        "skin_name": best_combo.input_skin.name,
        "written": total_written,
        "group_errors": errors,
        "contract": _serialize_combo(best_combo),
        "contract_history": _recent_contract_history(best_combo.input_skin.id),
    }


def handle_random_skin(session: Session, _payload: dict[str, Any]) -> dict[str, Any]:
    """Picks a uniformly random weapon skin from the local catalog DB, for
    webext/sidebar.js's Random Fetch button. Deliberately never calls out to
    Steam itself for this -- Random Fetch already drives Steam page loads on
    its own; it must not also hit Steam's search API just to decide where to
    go next.

    `steam_url` reuses mono_trade_table._steam_listing_url -- a listing URL
    needs a full "<name> (<wear>)" market_hash_name to resolve at all (Steam
    404s on the bare skin name), and that page then lists every wear
    condition of the weapon together regardless of which one is in the URL,
    which is exactly what lets Random Fetch cycle wears on it via the
    category_730_Exterior query param (see webext/sidebar.js)."""
    skin = session.scalars(select(Skin).order_by(func.random()).limit(1)).first()
    if skin is None:
        return {"ok": False, "error": "No skins in the catalog"}
    return {"ok": True, "skin_name": skin.name, "steam_url": mono_trade_table._steam_listing_url(skin)}


def handle_overview_chunk(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Build one independently measurable rarity/StatTrak overview batch,
    with input prices from payload["input_source"] (default "steam" -- see
    mono_trade_table.INPUT_SOURCES), the sidebar's market dropdown's
    persisted value (webext/overview.js reads it from browser.storage.local,
    since this runs in its own tab with no dropdown of its own)."""
    rarity = payload.get("rarity_name")
    stattrak = payload.get("stattrak")
    if rarity not in mono_trade_overview.tradeup.INPUT_RARITIES or not isinstance(stattrak, bool):
        return {"ok": False, "error": "Invalid overview batch"}
    return mono_trade_overview.build_overview(
        session, rarities=[rarity], stattrak_values=[stattrak], input_source=_resolved_input_source(payload)
    )


# Action names the sidebar's payload carries in its "action" field --
# "fetch_offers" (the default, for payloads with none, e.g. from before this
# field existed) is the always-on scrape+table refresh; "construct_contract"
# is the "Construct Contract" button. Both share the exact same payload
# shape and disk-write behavior, differing only in what they compute after.
# "fetch_csfloat_offers"/"construct_contract_csfloat" are fetch_offers'/
# construct_contract's CSFloat counterparts -- see handle_fetch_csfloat_offers
# and handle_construct_contract_csfloat.
_HANDLERS = {
    "fetch_offers": handle_fetch_offers,
    "fetch_csfloat_offers": handle_fetch_csfloat_offers,
    "construct_contract": handle_construct_contract,
    "construct_contract_csfloat": handle_construct_contract_csfloat,
    "overview": lambda session, payload: mono_trade_overview.build_overview(
        session, input_source=_resolved_input_source(payload)
    ),
    "overview_chunk": handle_overview_chunk,
    "random_skin": handle_random_skin,
    "skins_overview": lambda session, _payload: skins_overview.build_skins_overview(session),
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
