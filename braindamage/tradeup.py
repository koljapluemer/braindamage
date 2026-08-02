"""Trade-up contract simulator domain logic.

Implements the classic 10-in-1 weapon-skin trade-up contract (see
docs/skin-mechanics.md for the full mechanics writeup and sourcing). Knife/glove
crafting (Oct 2025 update) and Souvenir inputs (May 2026 update) are out of scope —
both need data/rules this module deliberately doesn't model yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import MarketItem, Skin
from .pricing import latest_prices

# Rarity ladder, ranked by color rather than name — weapon/knife/glove categories
# use different name strings for equivalent tiers (e.g. gloves top out at
# "Extraordinary", not "Covert"), though that divergence doesn't matter here since
# knives/gloves are excluded entirely. Verified against the live DB's distinct
# rarity_name/rarity_color pairs.
RARITY_LADDER: list[tuple[str, str]] = [
    ("Consumer Grade", "#b0c3d9"),
    ("Industrial Grade", "#5e98d9"),
    ("Mil-Spec Grade", "#4b69ff"),
    ("Restricted", "#8847ff"),
    ("Classified", "#d32ce6"),
    ("Covert", "#eb4b4b"),
]

# Covert is output-only in the classic path. Consumer Grade, despite never being a
# valid *output*, is still the bottom rung and a valid *input*.
INPUT_RARITIES: list[str] = [name for name, _ in RARITY_LADDER[:-1]]

_RANK_BY_COLOR = {color: rank for rank, (_, color) in enumerate(RARITY_LADDER)}
_COLOR_BY_NAME = {name: color for name, color in RARITY_LADDER}


def rarity_rank(rarity_color: str | None) -> int | None:
    """Ladder position of a rarity color, or None if it's not on the ladder
    (e.g. Contraband, or a knife/glove-only tier)."""
    if rarity_color is None:
        return None
    return _RANK_BY_COLOR.get(rarity_color)


def next_rarity(rarity_name: str) -> str | None:
    """The rarity one tier above `rarity_name`, or None if there isn't one
    (Covert) or `rarity_name` isn't on the ladder."""
    color = _COLOR_BY_NAME.get(rarity_name)
    if color is None:
        return None
    rank = _RANK_BY_COLOR[color]
    if rank + 1 >= len(RARITY_LADDER):
        return None
    return RARITY_LADDER[rank + 1][0]


# Standard wear buckets. A skin's actual float is clamped to its own
# [min_float, max_float] before this is applied, and not every skin has a
# MarketItem for every bucket — see resolve_market_item_by_float.
WEAR_BUCKETS: list[tuple[str, float, float]] = [
    ("Factory New", 0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn", 0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]


def wear_bucket_range(wear_name: str) -> tuple[float, float]:
    for name, lo, hi in WEAR_BUCKETS:
        if name == wear_name:
            return lo, hi
    raise ValueError(f"Unknown wear: {wear_name!r}")


def wear_for_float(f: float) -> str:
    """Standard exterior for a raw float, clamped to [0, 1]."""
    f = min(max(f, 0.0), 1.0)
    for name, lo, hi in WEAR_BUCKETS:
        if f <= hi:
            return name
    return WEAR_BUCKETS[-1][0]


# CS2's total Steam Community Market cut. A flat rate, not the precise
# per-component-with-minimums rounding Steam actually applies — good enough for a
# "basic EV", revisit if/when this needs cent-accurate numbers.
SELL_FEE_RATE = 0.15


# --- Contract state ----------------------------------------------------------


@dataclass
class ContractLine:
    market_item_id: str
    skin_id: str
    skin_name: str
    collection_id: str
    collection_name: str
    wear_name: str
    float_value: float
    quantity: int


@dataclass
class ContractState:
    rarity_name: str | None = None
    stattrak: bool = False
    lines: list[ContractLine] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def is_ready(self) -> bool:
        return self.total_quantity == 10


# --- Pure math -----------------------------------------------------------------


def average_float(lines: list[ContractLine]) -> float:
    """Plain mean float across all 10 inputs, weighted by quantity — the formula
    Valve actually uses (confirmed against docs/skin-mechanics.md, the Fandom
    wiki, and several independent open-source trade-up calculators), not a
    per-input normalized-to-0-1 average some marketing-blog sources claim."""
    total_quantity = sum(line.quantity for line in lines)
    if total_quantity == 0:
        raise ValueError("Cannot average the float of zero inputs")
    weighted_sum = sum(line.float_value * line.quantity for line in lines)
    return weighted_sum / total_quantity


def output_float(avg_float: float, min_out: float, max_out: float) -> float:
    """outFloat = minOut + (maxOut - minOut) * avg(input floats)."""
    return min_out + (max_out - min_out) * avg_float


def collection_probability(n_c: int, m_c: int) -> float:
    """Probability of one specific output skin in a collection that contributed
    n_c of the 10 inputs and has m_c eligible output skins at the target rarity."""
    if m_c <= 0:
        raise ValueError("A represented collection must have >=1 eligible output")
    return (n_c / 10) * (1 / m_c)


# --- Outcomes / results --------------------------------------------------------


@dataclass
class Outcome:
    skin_id: str
    skin_name: str
    collection_name: str
    probability: float
    predicted_float: float
    predicted_wear: str
    market_item_id: str | None
    gross_price: float | None
    net_price: float | None
    contribution: float


@dataclass
class SimulationResult:
    input_cost: float
    missing_input_price_names: list[str]
    outcomes: list[Outcome]
    expected_output_value: float
    expected_value: float
    roi: float | None
    missing_output_price_names: list[str]


# --- DB-facing queries -----------------------------------------------------------

_NON_WEAPON_CATEGORIES = ["Knives", "Gloves"]


def eligible_input_skins(session: Session, rarity_name: str, stattrak: bool) -> list[Skin]:
    """Weapon skins usable as trade-up inputs at `rarity_name`: excludes
    knives/gloves, requires a collection, and requires that collection to have
    >=1 skin at the next rarity tier (StatTrak-filtered too, if requested) —
    otherwise the input would be a dead end (the wiki's Tec-9 | Ossified / Aztec
    Collection example)."""
    target = next_rarity(rarity_name)
    if target is None:
        return []

    output_query = (
        select(Skin.collection_id)
        .where(Skin.rarity_name == target)
        .where(Skin.collection_id.is_not(None))
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .distinct()
    )
    output_query = _apply_variant_filter(output_query, stattrak)
    output_collection_ids = set(session.scalars(output_query).all())
    if not output_collection_ids:
        return []

    input_query = (
        select(Skin)
        .options(selectinload(Skin.collection))
        .where(Skin.rarity_name == rarity_name)
        .where(Skin.collection_id.in_(output_collection_ids))
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .order_by(Skin.name)
    )
    input_query = _apply_variant_filter(input_query, stattrak)
    return list(session.scalars(input_query).all())


def _apply_variant_filter(query, stattrak: bool):
    """A skin can only be used/produced in a StatTrak contract if a StatTrak
    variant of it exists; conversely a Souvenir-only skin (has_normal_variant is
    False for exactly one skin today, MP5-SD | Lab Rats) can't appear in a normal
    contract either."""
    if stattrak:
        return query.where(Skin.stattrak.is_(True))
    return query.where(Skin.has_normal_variant.is_(True))


@dataclass(frozen=True)
class SkinOption:
    """One searchable trade-up input choice — a skin at a specific rarity and
    StatTrak state. Deliberately plain data (no ORM refs) so it's cheap to build
    a combined list across every tier and cache it across Streamlit reruns."""

    skin_id: str
    skin_name: str
    collection_id: str
    collection_name: str
    rarity_name: str
    stattrak: bool
    min_float: float
    max_float: float

    @property
    def label(self) -> str:
        prefix = "StatTrak™ " if self.stattrak else ""
        return f"{prefix}{self.skin_name} — {self.collection_name} [{self.rarity_name}]"


def eligible_input_options(session: Session) -> list[SkinOption]:
    """Every valid trade-up input across every rarity and StatTrak state, as one
    flat searchable list. There's no rarity/StatTrak picker in the UI — a
    contract locks to whichever tier+StatTrak the first added item belongs to,
    so the search box needs to offer everything up front, not just one tier."""
    options: list[SkinOption] = []
    for rarity_name in INPUT_RARITIES:
        for stattrak in (False, True):
            for skin in eligible_input_skins(session, rarity_name, stattrak):
                options.append(
                    SkinOption(
                        skin_id=skin.id,
                        skin_name=skin.name,
                        collection_id=skin.collection_id,
                        collection_name=skin.collection.name,
                        rarity_name=rarity_name,
                        stattrak=stattrak,
                        min_float=skin.min_float if skin.min_float is not None else 0.0,
                        max_float=skin.max_float if skin.max_float is not None else 1.0,
                    )
                )
    return options


def resolve_market_item(
    session: Session, skin_id: str, wear_name: str, stattrak: bool
) -> MarketItem | None:
    """Looks up the MarketItem for a skin+wear+StatTrak combo. For Doppler/Gamma
    Doppler skins this is ambiguous (multiple phases share one wear+StatTrak
    combo) — arbitrarily returns one, since phase is randomly assigned on output
    regardless of input and isn't predictable either way."""
    query = (
        select(MarketItem)
        .where(MarketItem.skin_id == skin_id)
        .where(MarketItem.wear_name == wear_name)
        .where(MarketItem.stattrak.is_(stattrak))
        .where(MarketItem.souvenir.is_(False))
    )
    return session.scalars(query).first()


def resolve_market_item_by_float(
    session: Session, skin_id: str, stattrak: bool, target_float: float
) -> MarketItem | None:
    """Finds the MarketItem for `skin_id` whose wear bucket contains
    `target_float`. If that skin has no MarketItem for the exact bucket (some
    skins skip a wear tier), falls back to the available wear whose bucket
    midpoint is closest. Wear is *always* derived this way, never chosen
    independently — used both to resolve what a manually-entered input float
    actually buys, and to predict an output's wear/price from the computed
    output float."""
    query = (
        select(MarketItem.wear_name)
        .where(MarketItem.skin_id == skin_id)
        .where(MarketItem.stattrak.is_(stattrak))
        .where(MarketItem.souvenir.is_(False))
        .where(MarketItem.wear_name.is_not(None))
        .distinct()
    )
    available = set(session.scalars(query).all())
    if not available:
        return None

    target_wear = wear_for_float(target_float)
    if target_wear not in available:
        def midpoint_distance(wear_name: str) -> float:
            lo, hi = wear_bucket_range(wear_name)
            return abs((lo + hi) / 2 - target_float)

        target_wear = min(available, key=midpoint_distance)

    return resolve_market_item(session, skin_id, target_wear, stattrak)


def simulate_contract(session: Session, contract: ContractState) -> SimulationResult:
    """Runs a complete, ready (10-input) contract: collection-weighted output
    probabilities, predicted float/wear per outcome, latest known prices, and EV."""
    if not contract.is_ready or contract.rarity_name is None:
        raise ValueError("Contract needs exactly 10 inputs before it can be simulated")

    target_rarity = next_rarity(contract.rarity_name)
    if target_rarity is None:
        raise ValueError(f"{contract.rarity_name} has no next rarity tier")

    n_by_collection: dict[str, int] = {}
    collection_names: dict[str, str] = {}
    for line in contract.lines:
        n_by_collection[line.collection_id] = n_by_collection.get(line.collection_id, 0) + line.quantity
        collection_names[line.collection_id] = line.collection_name

    avg_float = average_float(contract.lines)

    # (skin, probability, collection_id) per eligible specific output.
    outcome_specs: list[tuple[Skin, float, str]] = []
    for collection_id, n_c in n_by_collection.items():
        output_query = (
            select(Skin)
            .where(Skin.collection_id == collection_id)
            .where(Skin.rarity_name == target_rarity)
            .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        )
        output_query = _apply_variant_filter(output_query, contract.stattrak)
        output_skins = list(session.scalars(output_query).all())
        m_c = len(output_skins)
        if m_c == 0:
            # Shouldn't happen if every line came from eligible_input_skins(), but
            # guard rather than silently drop a represented collection.
            raise ValueError(
                f"Collection {collection_names[collection_id]!r} has no eligible "
                f"output at {target_rarity!r} — contract is invalid"
            )
        probability = collection_probability(n_c, m_c)
        outcome_specs.extend((skin, probability, collection_id) for skin in output_skins)

    resolved: list[tuple[Skin, float, str, str, MarketItem | None, float]] = []
    for skin, probability, collection_id in outcome_specs:
        min_out = skin.min_float if skin.min_float is not None else 0.0
        max_out = skin.max_float if skin.max_float is not None else 1.0
        predicted = output_float(avg_float, min_out, max_out)
        predicted = min(max(predicted, min_out), max_out)
        market_item = resolve_market_item_by_float(session, skin.id, contract.stattrak, predicted)
        predicted_wear = market_item.wear_name if market_item else wear_for_float(predicted)
        resolved.append((skin, probability, collection_id, predicted_wear, market_item, predicted))

    input_ids = [line.market_item_id for line in contract.lines]
    output_ids = [mi.id for *_, mi, _ in resolved if mi is not None]
    prices = latest_prices(session, list({*input_ids, *output_ids}))

    input_cost = 0.0
    missing_input_price_names: list[str] = []
    for line in contract.lines:
        price = prices.get(line.market_item_id)
        if price is None:
            missing_input_price_names.append(f"{line.skin_name} ({line.wear_name})")
        else:
            input_cost += price * line.quantity

    outcomes: list[Outcome] = []
    missing_output_price_names: list[str] = []
    expected_output_value = 0.0
    for skin, probability, collection_id, predicted_wear, market_item, predicted in resolved:
        gross_price = prices.get(market_item.id) if market_item else None
        if gross_price is None:
            missing_output_price_names.append(f"{skin.name} ({predicted_wear})")
            net_price = None
            contribution = 0.0
        else:
            net_price = gross_price * (1 - SELL_FEE_RATE)
            contribution = probability * net_price
            expected_output_value += probability * gross_price
        outcomes.append(
            Outcome(
                skin_id=skin.id,
                skin_name=skin.name,
                collection_name=collection_names[collection_id],
                probability=probability,
                predicted_float=predicted,
                predicted_wear=predicted_wear,
                market_item_id=market_item.id if market_item else None,
                gross_price=gross_price,
                net_price=net_price,
                contribution=contribution,
            )
        )

    outcomes.sort(key=lambda o: o.probability, reverse=True)
    expected_value = sum(o.contribution for o in outcomes) - input_cost
    roi = expected_value / input_cost if input_cost > 0 else None

    return SimulationResult(
        input_cost=input_cost,
        missing_input_price_names=missing_input_price_names,
        outcomes=outcomes,
        expected_output_value=expected_output_value,
        expected_value=expected_value,
        roi=roi,
        missing_output_price_names=missing_output_price_names,
    )
