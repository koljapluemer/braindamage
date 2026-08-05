"""Trade-up contract simulator domain logic.

Implements the classic 10-in-1 weapon-skin trade-up contract (see
docs/skin-mechanics.md for the full mechanics writeup and sourcing). Knife/glove
crafting (Oct 2025 update) and Souvenir inputs (May 2026 update) are out of scope —
both need data/rules this module deliberately doesn't model yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pricing
from .models import Skin

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
# [min_float, max_float] before this is applied.
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
    skin_id: str
    skin_name: str
    collection_id: str
    collection_name: str
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


# --- Outcome risk stats ---------------------------------------------------------


def outcome_profits(result: SimulationResult) -> list[tuple[float, float]]:
    """(profit, probability) pairs, one per possible outcome of a simulated
    contract. Profit is what selling that single output nets versus what the 10
    inputs cost — a missing output price counts as $0 net, the same convention
    simulate_contract itself uses when it folds a priceless outcome into
    expected_value."""
    return [
        ((o.net_price if o.net_price is not None else 0.0) - result.input_cost, o.probability)
        for o in result.outcomes
    ]


def cvar(outcomes: list[tuple[float, float]], alpha: float = 0.05) -> float | None:
    """Conditional Value at Risk: the probability-weighted average profit within
    the worst `alpha` slice of the outcome distribution's probability mass.

    Outcomes are sorted by profit ascending and consumed from the bottom until
    `alpha` of probability mass is covered; an outcome straddling the cutoff
    contributes only its covered fraction (standard discrete-CVaR treatment).
    Returns None if `outcomes` carries no probability mass at all."""
    total_p = sum(p for _, p in outcomes)
    if total_p <= 0:
        return None

    remaining = alpha
    weighted_sum = 0.0
    covered = 0.0
    for profit, p in sorted(outcomes, key=lambda x: x[0]):
        if remaining <= 0:
            break
        take = min(p, remaining)
        weighted_sum += profit * take
        covered += take
        remaining -= take

    if covered <= 0:
        return None
    return weighted_sum / covered


# --- DB-facing queries -----------------------------------------------------------

_NON_WEAPON_CATEGORIES = ["Knives", "Gloves"]


def eligible_input_skins(session: Session, rarity_name: str, stattrak: bool) -> list[Skin]:
    """Weapon skins usable as trade-up inputs at `rarity_name`: excludes
    knives/gloves and souvenirs, requires a collection, and requires that
    collection to have >=1 skin at the next rarity tier (StatTrak-filtered too)
    — otherwise the input would be a dead end (the wiki's Tec-9 | Ossified /
    Aztec Collection example)."""
    target = next_rarity(rarity_name)
    if target is None:
        return []

    output_query = (
        select(Skin.collection_id)
        .where(Skin.rarity_name == target)
        .where(Skin.collection_id.is_not(None))
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .where(Skin.stattrak.is_(stattrak))
        .where(Skin.souvenir.is_(False))
        .distinct()
    )
    output_collection_ids = set(session.scalars(output_query).all())
    if not output_collection_ids:
        return []

    input_query = (
        select(Skin)
        .where(Skin.rarity_name == rarity_name)
        .where(Skin.collection_id.in_(output_collection_ids))
        .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
        .where(Skin.stattrak.is_(stattrak))
        .where(Skin.souvenir.is_(False))
        .order_by(Skin.name)
    )
    return list(session.scalars(input_query).all())


@dataclass(frozen=True)
class SkinOption:
    """One searchable trade-up input choice — a skin at a specific rarity and
    StatTrak state. Deliberately plain data (no ORM refs) so it's cheap to build
    a combined list across every tier and cache it across UI refreshes."""

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
                        collection_name=skin.collection_name,
                        rarity_name=rarity_name,
                        stattrak=stattrak,
                        min_float=skin.min_float if skin.min_float is not None else 0.0,
                        max_float=skin.max_float if skin.max_float is not None else 1.0,
                    )
                )
    return options


def _resolve_outcome_specs(
    session: Session, contract: ContractState, target_rarity: str
) -> tuple[list[tuple[Skin, float, str]], dict[str, str]]:
    """(skin, probability, collection_id) per eligible specific output for
    `contract`, plus a collection_id -> collection_name lookup -- the
    collection-weighted probability structure shared by simulate_contract and
    simulate_ev_curve (the latter re-prices these same outcomes at many
    hypothetical average input floats instead of just the contract's actual
    one)."""
    n_by_collection: dict[str, int] = {}
    collection_names: dict[str, str] = {}
    for line in contract.lines:
        n_by_collection[line.collection_id] = n_by_collection.get(line.collection_id, 0) + line.quantity
        collection_names[line.collection_id] = line.collection_name

    outcome_specs: list[tuple[Skin, float, str]] = []
    for collection_id, n_c in n_by_collection.items():
        output_query = (
            select(Skin)
            .where(Skin.collection_id == collection_id)
            .where(Skin.rarity_name == target_rarity)
            .where(Skin.category_name.not_in(_NON_WEAPON_CATEGORIES))
            .where(Skin.stattrak.is_(contract.stattrak))
            .where(Skin.souvenir.is_(False))
        )
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

    return outcome_specs, collection_names


def simulate_contract(session: Session, contract: ContractState) -> SimulationResult:
    """Runs a complete, ready (10-input) contract: collection-weighted output
    probabilities, predicted float/wear per outcome, latest known prices (read
    from each skin's JSON signals via braindamage.pricing), and EV."""
    if not contract.is_ready or contract.rarity_name is None:
        raise ValueError("Contract needs exactly 10 inputs before it can be simulated")

    target_rarity = next_rarity(contract.rarity_name)
    if target_rarity is None:
        raise ValueError(f"{contract.rarity_name} has no next rarity tier")

    avg_float = average_float(contract.lines)
    outcome_specs, collection_names = _resolve_outcome_specs(session, contract, target_rarity)

    resolved: list[tuple[Skin, float, str, str, float]] = []
    for skin, probability, collection_id in outcome_specs:
        min_out = skin.min_float if skin.min_float is not None else 0.0
        max_out = skin.max_float if skin.max_float is not None else 1.0
        predicted = output_float(avg_float, min_out, max_out)
        predicted = min(max(predicted, min_out), max_out)
        predicted_wear = wear_for_float(predicted)
        resolved.append((skin, probability, collection_id, predicted_wear, predicted))

    input_cost = 0.0
    missing_input_price_names: list[str] = []
    for line in contract.lines:
        wear = wear_for_float(line.float_value)
        price_info = pricing.latest_price_for_wear(line.skin_id, wear)
        if price_info is None:
            missing_input_price_names.append(f"{line.skin_name} ({wear})")
        else:
            price, _observed_at = price_info
            input_cost += price * line.quantity

    outcomes: list[Outcome] = []
    missing_output_price_names: list[str] = []
    expected_output_value = 0.0
    for skin, probability, collection_id, predicted_wear, predicted in resolved:
        price_info = pricing.latest_price_for_wear(skin.id, predicted_wear)
        if price_info is None:
            missing_output_price_names.append(f"{skin.name} ({predicted_wear})")
            gross_price = None
            net_price = None
            contribution = 0.0
        else:
            gross_price, _observed_at = price_info
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


# --- EV vs. average input float curve --------------------------------------------


@dataclass
class EvCurvePoint:
    """One sample of "what would this contract's EV be if its inputs averaged
    this float instead" -- the contract's actual skin *choices* (and their
    collection-weighted output probabilities) are held fixed; only the
    hypothetical average input float varies."""

    avg_float: float
    input_cost: float
    expected_revenue: float
    expected_value: float
    stdev: float


def average_float_achievable_range(session: Session, contract: ContractState) -> tuple[float, float]:
    """The [lo, hi] band `average_float(contract.lines)` could possibly fall in,
    given the specific skins (and quantities) already chosen for `contract` --
    each input line's float is bounded by its own skin's [min_float, max_float],
    so the quantity-weighted average is bounded by the same weighted average of
    those per-line bounds. This is what simulate_ev_curve sweeps, not the full
    [0, 1] range, since floats outside it aren't achievable with this exact
    contract's inputs."""
    total_quantity = sum(line.quantity for line in contract.lines)
    if total_quantity == 0:
        raise ValueError("Cannot compute an achievable float range for zero inputs")

    weighted_lo = 0.0
    weighted_hi = 0.0
    for line in contract.lines:
        skin = session.get(Skin, line.skin_id)
        lo = skin.min_float if skin is not None and skin.min_float is not None else line.float_value
        hi = skin.max_float if skin is not None and skin.max_float is not None else line.float_value
        weighted_lo += lo * line.quantity
        weighted_hi += hi * line.quantity

    return weighted_lo / total_quantity, weighted_hi / total_quantity


def simulate_ev_curve(session: Session, contract: ContractState, n_samples: int = 100) -> list[EvCurvePoint]:
    """Samples `n_samples` equally-spaced hypothetical average input floats
    across `contract`'s achievable range (see average_float_achievable_range),
    and for each, prices both sides with simple wear-bucket prices rather than
    per-cent float precision: every input line is priced at the wear bucket its
    own float range clamps `avg_float` to, and every possible outcome is priced
    at the wear bucket its predicted_float (via the real output_float formula)
    falls into. The spread (stdev) of an outcome's possible net prices around
    the sample's expected revenue is reported per-sample as the curve's error
    bar, since a single trade-up draws exactly one of many possible outputs.

    This intentionally does *not* use `contract.lines`' actual chosen floats --
    it's a "what if I sourced this same set of skins at a different average
    float" curve, independent of the specific float the user picked.
    """
    if not contract.is_ready or contract.rarity_name is None:
        raise ValueError("Contract needs exactly 10 inputs before its EV curve can be simulated")

    target_rarity = next_rarity(contract.rarity_name)
    if target_rarity is None:
        raise ValueError(f"{contract.rarity_name} has no next rarity tier")

    # skin_id -> (min_float, max_float, quantity), collapsed across lines sharing
    # a skin so a repeated skin isn't priced/clamped redundantly per sample.
    line_bounds: dict[str, tuple[float, float, int]] = {}
    for line in contract.lines:
        skin = session.get(Skin, line.skin_id)
        lo = skin.min_float if skin is not None and skin.min_float is not None else line.float_value
        hi = skin.max_float if skin is not None and skin.max_float is not None else line.float_value
        prev_lo, prev_hi, prev_qty = line_bounds.get(line.skin_id, (lo, hi, 0))
        line_bounds[line.skin_id] = (min(lo, prev_lo), max(hi, prev_hi), prev_qty + line.quantity)

    outcome_specs, _collection_names = _resolve_outcome_specs(session, contract, target_rarity)

    # One signal-file read per referenced skin, reused across every sample --
    # simulate_contract's per-lookup pricing.latest_price_for_wear would
    # otherwise re-read (and re-validate) the same JSON files n_samples times.
    input_price_cache = {skin_id: pricing.latest_prices_by_wear(skin_id) for skin_id in line_bounds}
    output_price_cache = {skin.id: pricing.latest_prices_by_wear(skin.id) for skin, _p, _c in outcome_specs}

    range_lo, range_hi = average_float_achievable_range(session, contract)
    if range_hi < range_lo:
        range_lo, range_hi = range_hi, range_lo
    if n_samples <= 1:
        samples = [range_lo]
    else:
        step = (range_hi - range_lo) / (n_samples - 1)
        samples = [range_lo + i * step for i in range(n_samples)]

    points: list[EvCurvePoint] = []
    for x in samples:
        input_cost = 0.0
        for skin_id, (lo, hi, qty) in line_bounds.items():
            clamped = min(max(x, lo), hi)
            wear = wear_for_float(clamped)
            price_info = input_price_cache[skin_id].get(wear)
            if price_info is not None:
                price, _observed_at = price_info
                input_cost += price * qty

        expected_revenue = 0.0
        weighted_net_prices: list[tuple[float, float]] = []
        for skin, probability, _collection_id in outcome_specs:
            min_out = skin.min_float if skin.min_float is not None else 0.0
            max_out = skin.max_float if skin.max_float is not None else 1.0
            predicted = output_float(x, min_out, max_out)
            predicted = min(max(predicted, min_out), max_out)
            wear = wear_for_float(predicted)
            price_info = output_price_cache[skin.id].get(wear)
            net_price = 0.0
            if price_info is not None:
                gross_price, _observed_at = price_info
                net_price = gross_price * (1 - SELL_FEE_RATE)
            expected_revenue += probability * net_price
            weighted_net_prices.append((net_price, probability))

        variance = sum(probability * (net_price - expected_revenue) ** 2 for net_price, probability in weighted_net_prices)
        stdev = variance**0.5

        points.append(
            EvCurvePoint(
                avg_float=x,
                input_cost=input_cost,
                expected_revenue=expected_revenue,
                expected_value=expected_revenue - input_cost,
                stdev=stdev,
            )
        )

    return points
