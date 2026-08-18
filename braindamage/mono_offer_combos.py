"""Finds the best mono trade-up combos buyable *right now* from real CSFloat
listings already on disk (braindamage.signals.MarketOfferSignal, written by
braindamage.postvalidate) -- unlike braindamage.mono_trades/tradeup, which price
inputs at an aggregate wear-bucket approximation, this picks 10 *specific*
still-fresh listings and prices the trade-up at what buying exactly those 10
would really cost right now.

Read-only against whatever's already on disk -- makes no network calls of its
own, so it can only ever be as fresh as the last braindamage.postvalidate run.
Offers older than MAX_OFFER_AGE are treated as stale and dropped, since a
CSFloat listing can be bought or delisted by someone else at any moment and
this app has no live confirmation the offer still stands.

The actual "pick 10 offers into the highest-EV combo" search is generic (see
braindamage.offer_combos) and shared with braindamage.steam_offer_combos --
this module only owns what's CSFloat-specific: reading/deduping/filtering the
on-disk offer pool.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from . import offer_combos, signals
from .models import Skin
from .signals import MarketOfferSignal, now_utc

MAX_OFFER_AGE = timedelta(hours=24)


def _fresh_offers_by_skin(session: Session) -> dict[str, list[MarketOfferSignal]]:
    """Every skin's on-disk market offers, deduped by listing_id (keeping each
    listing's most-recently-fetched snapshot), filtered to still-buyable
    (`listing_type == "buy_now"`) offers with a known float, younger than
    MAX_OFFER_AGE. Skins with no surviving offers are omitted entirely."""
    cutoff = now_utc() - MAX_OFFER_AGE
    result: dict[str, list[MarketOfferSignal]] = {}
    if not signals.SKINS_DIR.exists():
        return result

    for skin_dir in signals.SKINS_DIR.iterdir():
        if not skin_dir.is_dir():
            continue
        skin_id = skin_dir.name
        latest_by_listing: dict[str, MarketOfferSignal] = {}
        for offer in signals.read_market_offers(skin_id):
            if (
                offer.fetched_at < cutoff
                or offer.float_value is None
                or offer.listing_type != "buy_now"
            ):
                continue
            existing = latest_by_listing.get(offer.listing_id)
            if existing is None or offer.fetched_at > existing.fetched_at:
                latest_by_listing[offer.listing_id] = offer
        if latest_by_listing:
            result[skin_id] = list(latest_by_listing.values())
    return result


def best_combos_for_skin(
    session: Session, skin: Skin, offers: list[MarketOfferSignal], *, top_n: int = 3
) -> list[offer_combos.ComboResult]:
    return offer_combos.best_combos_for_skin(session, skin, offers, top_n=top_n)


def find_best_combos(session: Session, *, top_n: int = 3) -> list[offer_combos.ComboResult]:
    """The `top_n` highest real-EV mono trade-up combos buyable right now,
    across every input skin with enough fresh offers on disk -- pools each
    skin's own top `top_n` combos (see offer_combos.best_combos_for_skin) and
    re-ranks globally, so more than one of the returned combos can
    legitimately come from the same skin and share (and therefore mutually
    exclude) listings."""
    all_combos: list[offer_combos.ComboResult] = []
    for skin_id, offers in _fresh_offers_by_skin(session).items():
        skin = session.get(Skin, skin_id)
        if skin is None:
            continue
        all_combos.extend(offer_combos.best_combos_for_skin(session, skin, offers, top_n=top_n))
    all_combos.sort(key=lambda r: r.expected_value, reverse=True)
    return all_combos[:top_n]
