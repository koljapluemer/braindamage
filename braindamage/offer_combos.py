"""Generic "pick 10 real offers into a mono trade-up" combo search, shared by
braindamage.mono_offer_combos (CSFloat listings) and
braindamage.steam_offer_combos (Steam Community Market listings) -- the two
only differ in where their fresh, deduped offer pool comes from (see each
module's own `_fresh_offers_by_skin`), never in how a pool of offers gets
turned into ranked buy combos, which is what lives here.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pricing
from .models import Skin
from .tradeup import (
    SELL_FEE_RATE,
    collection_probability,
    next_rarity,
    normalized_float,
    output_float,
    wear_for_float,
)

REQUIRED_INPUTS = 10

# Combinations of 10 offers grow as C(n, 10) -- capped to the cheapest
# MAX_CANDIDATES_PER_SKIN fresh offers per skin so the search stays well under
# a second in pure Python (C(18, 10) = 43,758) rather than exploring every
# offer ever recorded for a skin. A cost-minimizing-for-its-output-tier combo
# overwhelmingly draws from the cheapest listings available, so this is a
# pragmatic bound, not an exact global search.
MAX_CANDIDATES_PER_SKIN = 18

_NON_WEAPON_CATEGORIES = ["Knives", "Gloves"]


class PricedFloatOffer(Protocol):
    """The only two fields the combo search actually needs from an offer --
    satisfied by both signals.MarketOfferSignal and signals.SteamOfferSignal
    without either knowing about this module."""

    price: float
    float_value: float | None


@dataclass
class OutputSpec:
    skin_id: str
    skin_name: str
    collection_name: str
    probability: float
    min_out: float
    max_out: float
    net_price_by_wear: dict[str, float]


@dataclass
class ComboOutcome:
    skin_id: str
    skin_name: str
    collection_name: str
    probability: float
    predicted_wear: str
    net_price: float | None
    contribution: float


@dataclass
class ComboResult:
    input_skin: Skin
    offers: list[PricedFloatOffer]
    avg_float: float
    real_cost: float
    outcomes: list[ComboOutcome]
    expected_value: float


def _output_specs(session: Session, skin: Skin) -> list[OutputSpec] | None:
    """Every output skin a mono trade-up of 10 `skin` could produce, with its
    collection-weighted probability and net sell price per wear -- None if
    `skin` isn't a valid trade-up input at all (wrong category, no next
    rarity, or its collection has no eligible output at that rarity)."""
    if skin.category_name in _NON_WEAPON_CATEGORIES or skin.souvenir or skin.collection_id is None:
        return None
    target_rarity = next_rarity(skin.rarity_name) if skin.rarity_name else None
    if target_rarity is None:
        return None

    query = (
        select(Skin)
        .where(Skin.collection_id == skin.collection_id)
        .where(Skin.rarity_name == target_rarity)
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .where(Skin.stattrak.is_(skin.stattrak))
        .where(Skin.souvenir.is_(False))
    )
    output_skins = list(session.scalars(query).all())
    if not output_skins:
        return None

    probability = collection_probability(REQUIRED_INPUTS, len(output_skins))
    specs = []
    for out_skin in output_skins:
        net_by_wear = {
            wear: price * (1 - SELL_FEE_RATE)
            for wear, (price, _observed_at) in pricing.latest_prices_by_wear(out_skin.id).items()
        }
        specs.append(
            OutputSpec(
                skin_id=out_skin.id,
                skin_name=out_skin.name,
                collection_name=out_skin.collection_name or "",
                probability=probability,
                min_out=out_skin.min_float if out_skin.min_float is not None else 0.0,
                max_out=out_skin.max_float if out_skin.max_float is not None else 1.0,
                net_price_by_wear=net_by_wear,
            )
        )
    return specs


def _evaluate_combo(
    skin: Skin, offers: tuple[PricedFloatOffer, ...], output_specs: list[OutputSpec]
) -> ComboResult:
    min_in = skin.min_float if skin.min_float is not None else 0.0
    max_in = skin.max_float if skin.max_float is not None else 1.0
    avg_float = sum(normalized_float(o.float_value, min_in, max_in) for o in offers) / len(offers)
    real_cost = sum(o.price for o in offers)

    outcomes: list[ComboOutcome] = []
    expected_revenue = 0.0
    for spec in output_specs:
        predicted = output_float(avg_float, spec.min_out, spec.max_out)
        predicted = min(max(predicted, spec.min_out), spec.max_out)
        wear = wear_for_float(predicted)
        net_price = spec.net_price_by_wear.get(wear)
        contribution = spec.probability * net_price if net_price is not None else 0.0
        expected_revenue += contribution
        outcomes.append(
            ComboOutcome(
                skin_id=spec.skin_id,
                skin_name=spec.skin_name,
                collection_name=spec.collection_name,
                probability=spec.probability,
                predicted_wear=wear,
                net_price=net_price,
                contribution=contribution,
            )
        )
    outcomes.sort(key=lambda o: o.probability, reverse=True)

    return ComboResult(
        input_skin=skin,
        offers=list(offers),
        avg_float=avg_float,
        real_cost=real_cost,
        outcomes=outcomes,
        expected_value=expected_revenue - real_cost,
    )


def best_combos_for_skin(
    session: Session, skin: Skin, offers: list[PricedFloatOffer], *, top_n: int = 3
) -> list[ComboResult]:
    """The `top_n` highest real-EV ways to pick exactly 10 of `offers` (fresh,
    still-buyable listings for `skin`) into a mono trade-up -- may include
    negative-EV combos if nothing better exists. Empty if `skin` isn't a valid
    trade-up input or fewer than REQUIRED_INPUTS fresh offers exist for it."""
    if len(offers) < REQUIRED_INPUTS:
        return []
    output_specs = _output_specs(session, skin)
    if not output_specs:
        return []

    pool = sorted(offers, key=lambda o: o.price)[:MAX_CANDIDATES_PER_SKIN]
    results = [
        _evaluate_combo(skin, combo, output_specs)
        for combo in itertools.combinations(pool, REQUIRED_INPUTS)
    ]
    results.sort(key=lambda r: r.expected_value, reverse=True)
    return results[:top_n]
