"""Static HTML report of mono trade-up contracts -- the terminal-CLI counterpart
to the Qt app's Contracts page/detail dialog, for a "find good contracts and look
at them" flow that doesn't need the app open. Reuses the exact same Contract rows
(and the exact same tradeup.py/mono_trades.py machinery that produced them) so the
numbers here always agree with the app by construction.

Deliberately a single self-contained .html file (inline CSS, hand-drawn SVG for
the EV-vs-float chart, no JS, no external requests) -- it's meant to be opened
once in a browser and read, not served or kept interactive.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from .models import Contract, Skin
from .tradeup import RARITY_LADDER, SELL_FEE_RATE

_RARITY_COLOR = dict(RARITY_LADDER)

# Status hues (dataviz skill's fixed, never-themed status palette) plus one
# categorical accent (violet) for the "every possible roll profits" case, which
# is a distinct state from merely "positive on average".
_GOOD = "#0ca30c"
_CRITICAL = "#d03b3b"
_WARNING = "#fab219"
_VIOLET_LIGHT = "#4a3aa7"
_VIOLET_DARK = "#9085e9"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _money(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}${value:,.2f}" if value >= 0 or not signed else f"-${abs(value):,.2f}"


def _pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    fmt = f"{value:+.1%}" if signed else f"{value:.1%}"
    return fmt


def _tip(icon_text: str, tooltip: str) -> str:
    return f'<span class="tip" title="{_esc(tooltip)}">{_esc(icon_text)} <span class="tip-icon">ⓘ</span></span>'


# --- Selection -----------------------------------------------------------------


@dataclass
class Selection:
    contracts: list[Contract]
    total_generated: int
    top_ev_pct_count: int
    top_net_win_count: int
    positive_cvar_count: int


def select_contracts(rows: list[Contract], *, top_n: int = 10) -> Selection:
    """The report's three overlapping shortlists -- highest ROI, highest absolute
    expected value, and every contract with a positive 5% CVaR (i.e. even the bad
    5% of outcomes is expected to profit) -- unioned, deduplicated by row id, and
    handed back sorted by ROI (EV%) descending, the report's display order."""
    by_ev_pct = sorted(rows, key=lambda c: c.roi if c.roi is not None else float("-inf"), reverse=True)[:top_n]
    by_net_win = sorted(rows, key=lambda c: c.expected_value, reverse=True)[:top_n]
    positive_cvar = [c for c in rows if c.cvar_5pct is not None and c.cvar_5pct > 0]

    merged: dict[str, Contract] = {}
    for c in (*by_ev_pct, *by_net_win, *positive_cvar):
        merged[c.id] = c

    ordered = sorted(merged.values(), key=lambda c: c.roi if c.roi is not None else float("-inf"), reverse=True)
    return Selection(
        contracts=ordered,
        total_generated=len(rows),
        top_ev_pct_count=len(by_ev_pct),
        top_net_win_count=len(by_net_win),
        positive_cvar_count=len(positive_cvar),
    )


# --- Per-contract derived numbers ------------------------------------------------


def _profit_chance(contract: Contract) -> float:
    """Probability the single roll this contract produces outsells its input
    cost -- summed straight from the outcome distribution, unlike CVaR (which
    only looks at the worst slice) or ROI (which is an average, not a chance)."""
    return sum(
        o["probability"]
        for o in contract.outcomes
        if (o["net_price"] if o["net_price"] is not None else 0.0) - contract.input_cost > 0
    )


def _worst_case_profit(contract: Contract) -> float:
    """The single worst possible roll's profit -- every outcome is a genuine
    possibility of this contract (however small its probability), so this is
    what you could actually walk away with on a bad day, as distinct from CVaR's
    probability-weighted average of the worst 5%."""
    if not contract.outcomes:
        return -contract.input_cost
    return min(
        (o["net_price"] if o["net_price"] is not None else 0.0) - contract.input_cost for o in contract.outcomes
    )


def _price_per_item(contract: Contract) -> float:
    total_qty = sum(line["quantity"] for line in contract.input_lines) or 1
    return contract.input_cost / total_qty


# --- EV curve chart (hand-drawn inline SVG, no chart library / no JS) -----------


def _merge_curve_points(contract: Contract) -> list[dict]:
    annotations = contract.ev_curve_annotations or []
    return [
        dict(point, **annotations[i]) if i < len(annotations) else point for i, point in enumerate(contract.ev_curve)
    ]


def _ev_curve_svg(contract: Contract) -> str:
    points = _merge_curve_points(contract)
    if len(points) < 2:
        return '<p class="muted">Not enough EV curve data to plot.</p>'

    width, height = 640, 220
    margin_left, margin_right, margin_top, margin_bottom = 58, 14, 12, 30
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def raw_x(p: dict) -> float:
        return p.get("raw_avg_float", p["avg_float"])

    xs = [raw_x(p) for p in points]
    evs = [p["expected_value"] for p in points]
    stdevs = [p["stdev"] for p in points]
    y_lows = [ev - sd for ev, sd in zip(evs, stdevs)]
    y_highs = [ev + sd for ev, sd in zip(evs, stdevs)]

    x_min, x_max = min(xs), max(xs)
    if x_max <= x_min:
        x_max = x_min + 1.0
    y_min, y_max = min(min(y_lows), 0.0), max(max(y_highs), 0.0)
    y_pad = (y_max - y_min) * 0.08 or 1.0
    y_min -= y_pad
    y_max += y_pad

    def X(x: float) -> float:
        return margin_left + (x - x_min) / (x_max - x_min) * plot_w

    def Y(y: float) -> float:
        return margin_top + plot_h - (y - y_min) / (y_max - y_min) * plot_h

    segments: list[str] = []
    for left, right in zip(points, points[1:]):
        worst = left.get("worst_profit", -1.0)
        ev = left["expected_value"]
        color = "var(--ev-guaranteed)" if worst >= 0 else ("var(--ev-good)" if ev >= 0 else "var(--ev-bad)")
        x1, y1 = X(raw_x(left)), Y(left["expected_value"])
        x2, y2 = X(raw_x(right)), Y(right["expected_value"])
        segments.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" />'
        )

    error_bars: list[str] = []
    for i, p in enumerate(points):
        if i % 5 != 0:
            continue
        x = X(raw_x(p))
        y_top = Y(p["expected_value"] + p["stdev"])
        y_bot = Y(p["expected_value"] - p["stdev"])
        error_bars.append(
            f'<line x1="{x:.1f}" y1="{y_top:.1f}" x2="{x:.1f}" y2="{y_bot:.1f}" '
            f'stroke="var(--ev-band)" stroke-width="1.5" />'
            f'<line x1="{x - 4:.1f}" y1="{y_top:.1f}" x2="{x + 4:.1f}" y2="{y_top:.1f}" '
            f'stroke="var(--ev-band)" stroke-width="1.5" />'
            f'<line x1="{x - 4:.1f}" y1="{y_bot:.1f}" x2="{x + 4:.1f}" y2="{y_bot:.1f}" '
            f'stroke="var(--ev-band)" stroke-width="1.5" />'
        )

    zero_y = Y(0.0)
    zero_line = (
        f'<line x1="{margin_left}" y1="{zero_y:.1f}" x2="{width - margin_right}" y2="{zero_y:.1f}" '
        f'stroke="var(--axis)" stroke-width="1" stroke-dasharray="4 3" />'
    )
    axis_x = (
        f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" '
        f'y2="{height - margin_bottom}" stroke="var(--axis)" stroke-width="1" />'
    )
    axis_y = (
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" '
        f'stroke="var(--axis)" stroke-width="1" />'
    )
    labels = (
        f'<text x="{margin_left}" y="{height - 6}" class="axis-label" text-anchor="start">{x_min:.3f}</text>'
        f'<text x="{width - margin_right}" y="{height - 6}" class="axis-label" text-anchor="end">{x_max:.3f}</text>'
        f'<text x="{margin_left - 6:.1f}" y="{Y(y_max) + 4:.1f}" class="axis-label" text-anchor="end">'
        f"${y_max:,.0f}</text>"
        f'<text x="{margin_left - 6:.1f}" y="{Y(y_min) + 4:.1f}" class="axis-label" text-anchor="end">'
        f"${y_min:,.0f}</text>"
        f'<text x="{width / 2:.0f}" y="{height - 2}" class="axis-title" text-anchor="middle">'
        f"Average input float (raw, normalized to a 0–1 scale per skin)</text>"
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="ev-chart" role="img" '
        f'aria-label="Expected value versus average input float, colored by risk category">'
        + zero_line
        + axis_x
        + axis_y
        + "".join(segments)
        + "".join(error_bars)
        + labels
        + "</svg>"
        '<div class="ev-legend">'
        '<span class="legend-item"><span class="swatch" style="background:var(--ev-guaranteed)"></span>'
        "Guaranteed profit (every outcome outsells cost)</span>"
        '<span class="legend-item"><span class="swatch" style="background:var(--ev-good)"></span>'
        "Positive EV</span>"
        '<span class="legend-item"><span class="swatch" style="background:var(--ev-bad)"></span>'
        "Negative EV</span>"
        '<span class="legend-item"><span class="swatch dashed"></span>EV = $0</span>'
        '<span class="legend-item"><span class="swatch" style="background:var(--ev-band)"></span>'
        "±1 stdev of possible outcome prices (every 5th sample)</span>"
        "</div>"
    )


# --- Tables ----------------------------------------------------------------------


def _rarity_dot(rarity_name: str | None) -> str:
    color = _RARITY_COLOR.get(rarity_name or "", "#888")
    return f'<span class="rarity-dot" style="background:{color}" title="{_esc(rarity_name or "Unknown")}"></span>'


def _input_skin_summary(contract: Contract, input_skin: Skin | None) -> str:
    """Facts about the input skin that hold true regardless of which buying
    range you pick -- name, collection, and the skin's own catalog metadata.
    Deliberately no wear/float/price here: those depend on which buying range
    (below) you actually buy into, so showing one arbitrary pick here would be
    exactly the kind of range-ambiguous number this report avoids."""
    input_line = contract.input_lines[0] if contract.input_lines else None
    if input_line is None:
        return '<p class="muted">No input line data.</p>'
    qty = sum(line["quantity"] for line in contract.input_lines)
    meta = ""
    if input_skin is not None:
        min_f = input_skin.min_float if input_skin.min_float is not None else 0.0
        max_f = input_skin.max_float if input_skin.max_float is not None else 1.0
        meta = (
            f" &nbsp;·&nbsp; {_tip('Skin float range', 'This skin’s own [min float, max float] — the range its exterior wear is drawn from, before normalization.')}: "
            f"{min_f:.3f} – {max_f:.3f}"
            f" &nbsp;·&nbsp; Weapon: {_esc(input_skin.weapon_name or '—')}"
            f" &nbsp;·&nbsp; Pattern: {_esc(input_skin.pattern_name or '—')}"
            f" &nbsp;·&nbsp; Category: {_esc(input_skin.category_name or '—')}"
        )
    return (
        f'<p class="meta-line"><strong>{_esc(input_line["skin_name"])}</strong> × {qty}'
        f" &nbsp;·&nbsp; Collection: {_esc(input_line['collection_name'])}{meta}</p>"
    )


def _possible_outputs_table(contract: Contract) -> str:
    """Every skin this contract could output and its probability -- both hold
    regardless of buying range, unlike wear/float/price (see the per-range
    breakdown below for those)."""
    rows = []
    for o in contract.outcomes:
        rows.append(
            "<tr>"
            f"<td class='num'>{_pct(o['probability'])}</td>"
            f"<td>{_esc(o['skin_name'])}</td>"
            f"<td>{_esc(o['collection_name'])}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        f"<th class='num'>{_tip('Prob.', 'Chance the trade-up produces this exact skin. Formula: (your inputs from this skin’s collection ÷ 10) × (1 ÷ number of eligible output skins in that collection at the next rarity). Independent of which buying range you pick — only the wear/price per skin depends on that.')}</th>"
        f"<th>{_tip('Skin', 'One specific skin this contract could output — every row is a different possible result, not a guarantee.')}</th>"
        "<th>Collection</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _range_input_table(range_detail) -> str:
    rows = []
    for inp in range_detail.inputs:
        rows.append(
            "<tr>"
            f"<td>{_esc(inp.skin_name)}</td>"
            f"<td>{_esc(inp.wear_name)}</td>"
            f"<td class='num'>{inp.quantity}</td>"
            f"<td class='num'>{_money(inp.unit_price) if inp.unit_price is not None else '—'}</td>"
            f"<td class='num'>{_money(inp.line_cost) if inp.line_cost is not None else '—'}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        f"<th>{_tip('Skin', 'Input skin bought for this range.')}</th>"
        f"<th>{_tip('Buy at wear', 'Which wear bucket this input needs to land in to buy into this range — constant across the whole range by construction.')}</th>"
        f"<th class='num'>Qty</th>"
        f"<th class='num'>{_tip('Unit price', 'Market price for one copy of this skin at this wear.')}</th>"
        f"<th class='num'>{_tip('Line cost', 'Unit price × quantity.')}</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _range_outcomes_table(range_detail) -> str:
    rows = []
    for o in range_detail.outcomes:
        profit = (o.net_price if o.net_price is not None else 0.0) - range_detail.input_cost
        profit_class = "good" if profit > 0 else ("bad" if profit < 0 else "")
        float_span = (
            f"{o.predicted_float_low:.4f}"
            if abs(o.predicted_float_high - o.predicted_float_low) < 1e-6
            else f"{o.predicted_float_low:.4f} – {o.predicted_float_high:.4f}"
        )
        rows.append(
            "<tr>"
            f"<td class='num'>{_pct(o.probability)}</td>"
            f"<td>{_esc(o.skin_name)}</td>"
            f"<td>{_esc(o.predicted_wear)}</td>"
            f"<td class='num'>{float_span}</td>"
            f"<td class='num'>{_money(o.gross_price) if o.gross_price is not None else '—'}</td>"
            f"<td class='num'>{_money(o.net_price) if o.net_price is not None else '—'}</td>"
            f"<td class='num {profit_class}'>{_money(profit, signed=True)}</td>"
            f"<td class='num'>{_money(o.contribution)}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        f"<th class='num'>Prob.</th>"
        f"<th>Skin</th>"
        f"<th>{_tip('Wear bucket', 'Wear this output lands in when bought into this range — constant across the whole range by construction.')}</th>"
        f"<th class='num'>{_tip('Predicted float', 'This output’s own float, remapped through its [min float, max float] from wherever in this range your average input float actually lands — a span, not one number, since it still moves within the range even though wear/price don’t.')}</th>"
        f"<th class='num'>{_tip('Sale price (gross)', 'What this skin currently sells for on Steam before the sell fee is deducted.')}</th>"
        f"<th class='num'>{_tip(f'Sale price (net, {SELL_FEE_RATE:.0%} fee)', f'What you’d actually receive selling this skin: gross price × (1 − {SELL_FEE_RATE:.0%}) = gross price × {1 - SELL_FEE_RATE:.2f}.')}</th>"
        f"<th class='num'>{_tip('Profit if rolled', 'Net sale price minus this range’s input cost — what you’d walk away with if this exact outcome were rolled after buying into this range.')}</th>"
        f"<th class='num'>{_tip('Contribution', 'This row’s slice of this range’s expected revenue: Probability × Net Price.')}</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _range_classification(detail) -> str:
    """Fresh risk classification from a RangeDetail's own priced outcomes --
    deliberately not trusting Contract.optimization_ranges' stored 'outcome'
    field, which was classified against whatever prices were on disk at
    generation time and can have gone stale since."""
    if detail.worst_profit >= 0:
        return "Guaranteed profit"
    return "Positive EV" if detail.expected_value >= 0 else "Negative EV"


def _range_roi(detail) -> float | None:
    return detail.expected_value / detail.input_cost if detail.input_cost > 0 else None


def _range_cvar(detail) -> float | None:
    from .tradeup import cvar

    pairs = [
        ((o.net_price if o.net_price is not None else 0.0) - detail.input_cost, o.probability)
        for o in detail.outcomes
    ]
    return cvar(pairs, alpha=0.05)


def evaluate_ranges(contract: Contract, session: Session):
    """(range_dict, RangeDetail) for every buying-float range this contract's
    EV curve collapsed into, each priced fresh against whatever's on disk right
    now -- `range_dict`'s float boundaries are structural (skin wear buckets,
    which don't change) so they're trusted as-is, but every priced number is
    recomputed rather than read from the contract's possibly-stale stored
    snapshot (see models.Contract: optimization_ranges is "never recomputed ad
    hoc"). Returns `[]` if this contract has no buying-range data at all."""
    ranges = contract.optimization_ranges or []
    if not ranges:
        return []

    from . import contracts as contracts_module
    from . import tradeup

    contract_state = contracts_module.state_from_row(contract)
    return [
        (r, tradeup.evaluate_contract_range(session, contract_state, r["min_normalized_float"], r["max_normalized_float"]))
        for r in ranges
    ]


def _range_breakdown(range_evals: list) -> str:
    """Detailed, self-consistent stats per buying-float range: for each range,
    what wear you're buying the input at and what it costs, and for every
    possible output, its wear bucket, predicted-float span, and sale price
    (gross and net) *at that same range* -- replaces a single ambiguous
    "possible outcomes" table (which could only ever reflect one arbitrary
    float pick) with one table per range, so every number on the page is
    traceable to a specific, named buying range. Displayed best (highest
    fresh EV) first, regardless of the stored ranges' original order."""
    if not range_evals:
        return '<p class="muted">No buying-range data (needs a complete, priced simulation).</p>'

    ordered = sorted(range_evals, key=lambda pair: pair[1].expected_value, reverse=True)
    sections = []
    for i, (r, detail) in enumerate(ordered, start=1):
        classification = _range_classification(detail)
        roi = _range_roi(detail)
        cvar_5pct = _range_cvar(detail)
        roi_class = "good" if (roi or 0) > 0 else ("bad" if (roi or 0) < 0 else "")
        cvar_class = "good" if (cvar_5pct or 0) > 0 else ("bad" if (cvar_5pct or 0) < 0 else "")
        label = "Best" if i == 1 else f"Alternative {i - 1}"
        float_bounds_text = f"{r['min_float']:.5f} – {r['max_float']:.5f}"
        float_bounds_tip = _tip(
            float_bounds_text,
            "The [min, max] average-input-float band that all produces this same expected outcome and "
            "price — see the EV chart above for the full curve this range was collapsed from.",
        )
        sections.append(
            f'<div class="range-section">'
            f'<p class="range-heading"><span class="range-badge">{_esc(label)}</span> '
            f"buy at average input float "
            f"{float_bounds_tip}"
            f' &nbsp;·&nbsp; <span class="tag">{_esc(classification)}</span>'
            f" &nbsp;·&nbsp; Expected price {_money(detail.expected_revenue)}"
            f" &nbsp;·&nbsp; ROI <span class='{roi_class}'>{_pct(roi, signed=True) if roi is not None else '—'}</span>"
            f" &nbsp;·&nbsp; CVaR (5%) <span class='{cvar_class}'>{_money(cvar_5pct, signed=True) if cvar_5pct is not None else '—'}</span>"
            f" &nbsp;·&nbsp; Chance of profit {detail.profit_chance:.1%}"
            f" &nbsp;·&nbsp; Worst case <span class='{'bad' if detail.worst_profit < 0 else 'good'}'>{_money(detail.worst_profit, signed=True)}</span>"
            "</p>"
            f"{_range_input_table(detail)}"
            f"{_range_outcomes_table(detail)}"
            "</div>"
        )
    return "".join(sections)


# --- Per-contract card -------------------------------------------------------------


def _metric_card(label: str, tooltip: str, value: str, css_class: str = "") -> str:
    return (
        f'<div class="metric-card"><div class="metric-label">{_tip(label, tooltip)}</div>'
        f'<div class="metric-value {css_class}">{value}</div></div>'
    )


def _contract_card(contract: Contract, session: Session, in_ev_top: bool, in_net_top: bool) -> str:
    input_skin_id = contract.input_lines[0]["skin_id"] if contract.input_lines else None
    input_skin = session.get(Skin, input_skin_id) if input_skin_id else None

    variant = "StatTrak™ " if contract.stattrak else ""
    input_line = contract.input_lines[0] if contract.input_lines else None
    title = f"10x {variant}{input_line['skin_name']}" if input_line else "Mono trade-up"

    # Every top-level number below comes from ONE fresh evaluation of the best
    # (highest-EV) buying-float range, priced against whatever's on disk right
    # now -- never from Contract.expected_value/roi/cvar_5pct/input_cost, which
    # are a snapshot from whenever this contract was last simulated and can
    # have gone stale if prices changed since (see models.Contract:
    # optimization_ranges is "never recomputed ad hoc"). Mixing a stale
    # snapshot with a fresh one is exactly how you get a contract that claims
    # +130% EV and 0% chance of profit in the same breath.
    range_evals = evaluate_ranges(contract, session)
    has_range_data = bool(range_evals)

    if has_range_data:
        best_range, best_detail = max(range_evals, key=lambda pair: pair[1].expected_value)
        input_cost = best_detail.input_cost
        expected_value = best_detail.expected_value
        roi = _range_roi(best_detail)
        cvar_5pct = _range_cvar(best_detail)
        expected_output_value = sum(
            o.probability * o.gross_price for o in best_detail.outcomes if o.gross_price is not None
        )
        total_qty = sum(inp.quantity for inp in best_detail.inputs) or 1
        price_per_item = input_cost / total_qty
        profit_chance = best_detail.profit_chance
        worst_case = best_detail.worst_profit
        incomplete = any(inp.unit_price is None for inp in best_detail.inputs) or any(
            o.gross_price is None for o in best_detail.outcomes
        )
    else:
        # No buying-range data at all (an incomplete/unpriced simulation) --
        # fall back to this contract's raw stored simulation result, which in
        # that case is exactly what Contract.outcomes/input_lines already are
        # (upsert_contract only substitutes the "best range" numbers when a
        # complete, priced simulation produced buying-range data to begin with).
        input_cost = contract.input_cost
        expected_value = contract.expected_value
        roi = contract.roi
        cvar_5pct = contract.cvar_5pct
        expected_output_value = contract.expected_output_value
        price_per_item = _price_per_item(contract)
        profit_chance = _profit_chance(contract)
        worst_case = _worst_case_profit(contract)
        incomplete = True

    ev_class = "good" if expected_value > 0 else ("bad" if expected_value < 0 else "")
    roi_class = "good" if (roi or 0) > 0 else ("bad" if (roi or 0) < 0 else "")

    # Shortlisting badges: EV%/net-$ just explain why this contract made the
    # cut (harmless even if ranking shifts slightly after a fresh reprice).
    # CVaR+ is a mathematical claim ("even the worst 5% of outcomes profit"),
    # which flatly contradicts a low chance-of-profit or negative EV -- so
    # unlike the other two, it's re-checked against the fresh cvar_5pct just
    # computed above rather than trusted from selection time.
    badges = []
    if in_ev_top:
        badges.append('<span class="tag tag-ev">Top 10 EV%</span>')
    if in_net_top:
        badges.append('<span class="tag tag-net">Top 10 net $</span>')
    if cvar_5pct is not None and cvar_5pct > 0:
        badges.append('<span class="tag tag-cvar">CVaR+</span>')

    warning = ""
    if incomplete:
        warning = (
            '<p class="warning">⚠ Some input/output prices are missing on disk — numbers above treat '
            "those as $0, so they may be incomplete.</p>"
        )

    summary_line = (
        "<summary>"
        f'{_rarity_dot(contract.rarity_name)}<span class="contract-title">{_esc(title)}</span>'
        f'<span class="chip">Input {_money(input_cost)}</span>'
        f'<span class="chip {ev_class}">EV {_money(expected_value, signed=True)}</span>'
        f'<span class="chip {roi_class}">{_pct(roi, signed=True) if roi is not None else "—"} ROI</span>'
        f'<span class="chip">{profit_chance:.0%} chance of profit</span>'
        f'<span class="chip bad">Worst case {_money(worst_case, signed=True)}</span>'
        + "".join(badges)
        + "</summary>"
    )

    metrics = (
        '<div class="metrics-grid">'
        + _metric_card(
            "Input cost",
            "Total market cost of the 10 input skins, bought at the best available float range "
            "(see the buying-range breakdown below). Does not include Steam’s sell fee — that’s only "
            "charged when you sell an output.",
            _money(input_cost),
        )
        + _metric_card(
            "Expected value",
            "Probability-weighted average profit at the best buying range: the sum of every possible "
            f"outcome’s (probability × net sell price after the {SELL_FEE_RATE:.0%} Steam fee), minus input "
            "cost. Positive means the contract is profitable on average; it says nothing about how risky "
            "any single roll is.",
            _money(expected_value, signed=True),
            ev_class,
        )
        + _metric_card(
            "ROI (EV%)",
            "Return on investment at the best buying range: Expected Value ÷ Input Cost, as a percentage. "
            "How much profit you’d expect to make relative to what you spent, on average across many rolls.",
            _pct(roi, signed=True) if roi is not None else "—",
            roi_class,
        )
        + _metric_card(
            "CVaR (5%)",
            "Conditional Value at Risk at the best buying range: the average profit across just the worst "
            "5% of possible outcomes (weighted by probability). A downside-risk measure — a very negative "
            "CVaR means the bad-luck rolls are painful even if Expected Value looks fine.",
            _money(cvar_5pct, signed=True) if cvar_5pct is not None else "—",
            "good" if (cvar_5pct or 0) > 0 else ("bad" if (cvar_5pct or 0) < 0 else ""),
        )
        + _metric_card(
            "Chance of profit",
            "Probability-weighted sum of every outcome that outsells the input cost, at the same best "
            "buying range as Expected Value/ROI/CVaR above — i.e. the odds this specific buy makes money "
            "at all. Not the same as CVaR (which only looks at the worst 5%) or ROI (an average, not a chance).",
            f"{profit_chance:.1%}",
        )
        + _metric_card(
            "Worst-case loss",
            "The single worst possible roll’s profit at that same best buying range — the lowest (net sale "
            "price − input cost) across every possible outcome, however small its probability. Unlike CVaR "
            "this isn’t probability-weighted, it’s the literal floor.",
            _money(worst_case, signed=True),
            "bad" if worst_case < 0 else "good",
        )
        + _metric_card(
            "Expected output value",
            "Probability-weighted average of the gross (pre-fee) sale price across every possible outcome — "
            "what Expected Value looks like before both the Steam sell fee and the input cost are subtracted out.",
            _money(expected_output_value),
        )
        + _metric_card(
            "Price per item",
            "Input cost divided by 10 — the per-unit price of the single cheapest-priced input skin this "
            "mono trade is built from.",
            _money(price_per_item),
        )
        + "</div>"
    )

    freshness = f"Last simulated: {contract.last_simulated_at:%Y-%m-%d %H:%M UTC}"

    body = (
        f'<div class="contract-body">'
        f'<p class="subhead">{_esc(contract.rarity_name)} → {_esc(contract.target_rarity_name)}'
        f' &nbsp;·&nbsp; {"StatTrak™" if contract.stattrak else "Normal"}'
        f' &nbsp;·&nbsp; Collection: {_esc(input_line["collection_name"]) if input_line else "—"}'
        f' &nbsp;·&nbsp; <span class="muted">{freshness}</span></p>'
        f"{warning}"
        f"{metrics}"
        f'<h4>{_tip("Input skin", "The skin this mono trade buys 10 copies of — facts here hold regardless of buying range; see the breakdown below for wear/price per range.")}</h4>'
        f"{_input_skin_summary(contract, input_skin)}"
        f'<h4>{_tip("Possible outputs", "Every specific skin this contract could output and its probability — both hold regardless of buying range.")}</h4>'
        f"{_possible_outputs_table(contract)}"
        f'<h4>{_tip("Expected value vs. average input float", "How this contract’s EV would change if its inputs averaged a different float — the contract’s actual skin choices are held fixed, only the hypothetical average input float varies. Computed once at simulation time; the buying-range breakdown below is what’s freshly re-priced.")}</h4>'
        f"{_ev_curve_svg(contract)}"
        f'<h4>{_tip("Buying-range breakdown", "Detailed, self-consistent stats for each buying-float range this contract’s EV curve collapses into (best first): which wear bucket you’re buying the input at and what it costs, and for every possible output, its wear bucket, predicted-float span, and sale price at that same range — all priced fresh against current on-disk prices.")}</h4>'
        f"{_range_breakdown(range_evals)}"
        "</div>"
    )

    return f'<details class="contract-card">{summary_line}{body}</details>'


# --- Page ---------------------------------------------------------------------


_CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-secondary: #52514e;
  --muted: #898781;
  --border: rgba(11,11,11,0.10);
  --axis: #c3c2b7;
  --grid: #e1e0d9;
  --good: #0ca30c;
  --bad: #d03b3b;
  --warning: #fab219;
  --ev-guaranteed: #4a3aa7;
  --ev-good: #0ca30c;
  --ev-bad: #d03b3b;
  --ev-band: rgba(42,120,214,0.45);
  --tag-ev-bg: #e6f6e6;
  --tag-net-bg: #e6eefc;
  --tag-cvar-bg: #f3ecfb;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-secondary: #c3c2b7;
    --muted: #898781;
    --border: rgba(255,255,255,0.10);
    --axis: #383835;
    --grid: #2c2c2a;
    --ev-guaranteed: #9085e9;
    --ev-band: rgba(57,135,229,0.5);
    --tag-ev-bg: #123312;
    --tag-net-bg: #10203a;
    --tag-cvar-bg: #241a35;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 16px 64px;
  background: var(--page);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.4;
}
.wrap { max-width: 1080px; margin: 0 auto; }
header.report-header { margin-bottom: 20px; }
header.report-header h1 { font-size: 1.4rem; margin: 0 0 4px; }
header.report-header p { margin: 2px 0; color: var(--ink-secondary); font-size: 0.92rem; }
.muted { color: var(--muted); }
.good { color: var(--good); }
.bad { color: var(--bad); }

.tip { text-decoration: underline dotted var(--muted); cursor: help; }
.tip-icon { font-size: 0.85em; color: var(--muted); }

.contract-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 10px;
  padding: 0;
}
.contract-card > summary {
  list-style: none;
  cursor: pointer;
  padding: 10px 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.contract-card > summary::-webkit-details-marker { display: none; }
.contract-card > summary::before {
  content: "\\25B8";
  color: var(--muted);
  font-size: 0.8rem;
  width: 1em;
}
.contract-card[open] > summary::before { content: "\\25BE"; }

.rarity-dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
.contract-title { font-weight: 600; margin-right: 4px; }
.chip {
  font-size: 0.82rem;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--page);
  border: 1px solid var(--border);
  color: var(--ink-secondary);
  white-space: nowrap;
}
.chip.good { color: var(--good); border-color: var(--good); }
.chip.bad { color: var(--bad); border-color: var(--bad); }

.tag {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  white-space: nowrap;
}
.tag { background: var(--page); border: 1px solid var(--border); color: var(--ink-secondary); }
.tag-ev { background: var(--tag-ev-bg); color: var(--good); border: none; }
.tag-net { background: var(--tag-net-bg); color: #2a78d6; border: none; }
.tag-cvar { background: var(--tag-cvar-bg); color: var(--ev-guaranteed); border: none; }

.contract-body { padding: 4px 16px 16px; border-top: 1px solid var(--border); }
.subhead { color: var(--ink-secondary); font-size: 0.88rem; }
.note { color: var(--muted); font-size: 0.8rem; font-style: italic; margin: 4px 0; }
.warning { color: var(--warning); font-size: 0.88rem; }

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  margin: 10px 0 16px;
}
.metric-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}
.metric-label { font-size: 0.76rem; color: var(--muted); margin-bottom: 3px; }
.metric-value { font-size: 1.05rem; font-weight: 600; }

h4 { margin: 18px 0 6px; font-size: 0.95rem; }
.meta-line { font-size: 0.82rem; color: var(--ink-secondary); margin: 4px 0 0; }

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
}
.data-table th, .data-table td {
  padding: 4px 8px;
  border-bottom: 1px solid var(--grid);
  text-align: left;
}
.data-table th { color: var(--muted); font-weight: 600; font-size: 0.78rem; }
.data-table td.num, .data-table th.num { text-align: right; }
.data-table tbody tr:hover { background: var(--page); }

.ev-chart { width: 100%; height: auto; display: block; }
.axis-label { font-size: 9px; fill: var(--muted); }
.axis-title { font-size: 10px; fill: var(--muted); }

.ev-legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 4px; font-size: 0.78rem; color: var(--ink-secondary); }
.legend-item { display: inline-flex; align-items: center; gap: 4px; }
.swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; background: var(--muted); }
.swatch.dashed { background: repeating-linear-gradient(90deg, var(--axis) 0 4px, transparent 4px 7px); }

.range-section {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px 12px;
  margin-bottom: 10px;
}
.range-section:last-child { margin-bottom: 0; }
.range-heading { font-size: 0.85rem; margin: 2px 0 8px; }
.range-badge {
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.02em;
  color: var(--ink-secondary);
}
.range-section .data-table { margin-bottom: 8px; }
.range-section .data-table:last-child { margin-bottom: 0; }

footer { margin-top: 24px; color: var(--muted); font-size: 0.8rem; }
"""


def render_report(selection: Selection, session: Session, *, max_input_cost: float) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    top_ev_ids = {c.id for c in sorted(
        selection.contracts, key=lambda c: c.roi if c.roi is not None else float("-inf"), reverse=True
    )[: selection.top_ev_pct_count]}
    top_net_ids = {c.id for c in sorted(selection.contracts, key=lambda c: c.expected_value, reverse=True)[
        : selection.top_net_win_count
    ]}

    cards = "".join(
        _contract_card(c, session, c.id in top_ev_ids, c.id in top_net_ids) for c in selection.contracts
    )

    if not selection.contracts:
        cards = '<p class="muted">No contracts matched — try raising --max-input-cost, or check that skins have priced signals.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mono trade-up contracts — max ${max_input_cost:,.2f}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="report-header">
<h1>Mono trade-up contracts</h1>
<p>Max input cost: <strong>{_money(max_input_cost)}</strong> &nbsp;·&nbsp; Generated {generated_at}</p>
<p>{selection.total_generated} mono trade(s) simulated under budget → {len(selection.contracts)} shown below:
top {selection.top_ev_pct_count} by EV%, top {selection.top_net_win_count} by net win $, and
{selection.positive_cvar_count} with positive CVaR (5%) — overlap deduplicated, sorted by EV% descending.</p>
</header>
<main>
{cards}
</main>
<footer>Prices are whatever was already on disk when this report was generated — no live price fetch was performed.</footer>
</div>
</body>
</html>
"""
