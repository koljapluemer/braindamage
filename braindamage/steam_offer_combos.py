"""Finds the best mono trade-up combos buyable *right now* from real Steam
Community Market listings already on disk (braindamage.signals.SteamOfferSignal,
written by braindamage.steam_offers_host from the companion Firefox extension) --
the Steam-market counterpart to braindamage.mono_offer_combos, which does the
same thing against CSFloat listings instead.

Read-only against whatever's already on disk -- makes no network calls of its
own, so it can only ever be as fresh as the last time the extension's Fetch
button was clicked. Offers older than MAX_OFFER_AGE are treated as stale and
dropped, same rationale as mono_offer_combos: a listing can be bought or
delisted by someone else at any moment and this app has no live confirmation
the offer still stands.

Steam's listing page exposes no stable per-listing ID, so offers are deduped
by (float_value, pattern_seed, price) instead of a real listing_id -- two
genuinely different real listings landing on the exact same 9-decimal float
is effectively impossible, so a collision here only ever means the same
physical listing got re-observed (e.g. re-scraping an unchanged page), which
is exactly the case this should collapse.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from . import offer_combos, signals
from .models import Skin
from .signals import SteamOfferSignal, now_utc

MAX_OFFER_AGE = timedelta(hours=24)


def _fresh_offers_by_skin(session: Session) -> dict[str, list[SteamOfferSignal]]:
    """Every skin's on-disk Steam offers, deduped by (float_value,
    pattern_seed, price) -- keeping each key's most-recently-fetched
    snapshot -- filtered to offers with a known float, younger than
    MAX_OFFER_AGE. No listing_type filter (unlike CSFloat's): everything
    Steam's listing page renders is buy-now by construction. Skins with no
    surviving offers are omitted entirely."""
    cutoff = now_utc() - MAX_OFFER_AGE
    result: dict[str, list[SteamOfferSignal]] = {}
    if not signals.SKINS_DIR.exists():
        return result

    for skin_dir in signals.SKINS_DIR.iterdir():
        if not skin_dir.is_dir():
            continue
        skin_id = skin_dir.name
        latest_by_key: dict[tuple[float, int | None, float], SteamOfferSignal] = {}
        for offer in signals.read_steam_offers(skin_id):
            if offer.fetched_at < cutoff or offer.float_value is None:
                continue
            key = (offer.float_value, offer.pattern_seed, offer.price)
            existing = latest_by_key.get(key)
            if existing is None or offer.fetched_at > existing.fetched_at:
                latest_by_key[key] = offer
        if latest_by_key:
            result[skin_id] = list(latest_by_key.values())
    return result


def find_best_combos(session: Session, *, top_n: int = 3) -> list[offer_combos.ComboResult]:
    """The `top_n` highest real-EV mono trade-up combos buyable right now,
    across every input skin with enough fresh Steam offers on disk -- see
    braindamage.mono_offer_combos.find_best_combos for the pooling/ranking
    rationale, identical here."""
    all_combos: list[offer_combos.ComboResult] = []
    for skin_id, offers in _fresh_offers_by_skin(session).items():
        skin = session.get(Skin, skin_id)
        if skin is None:
            continue
        all_combos.extend(offer_combos.best_combos_for_skin(session, skin, offers, top_n=top_n))
    all_combos.sort(key=lambda r: r.expected_value, reverse=True)
    return all_combos[:top_n]
