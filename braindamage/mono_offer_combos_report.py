"""Static HTML report of braindamage.mono_offer_combos' results -- the best
mono trade-up combos actually buyable right now from fresh CSFloat listings
already on disk. Same "single self-contained file, no JS" shape as
braindamage.report/tradeup_buys_report, and reuses their escaping/formatting
helpers so numbers render identically across every report this app writes.
"""

from __future__ import annotations

from datetime import datetime

from . import steam_fees
from .mono_offer_combos import MAX_OFFER_AGE
from .offer_combos import ComboResult
from .report import _esc, _money, _pct
from .signals import now_utc
from .tradeup import RARITY_LADDER

_RARITY_COLOR = dict(RARITY_LADDER)

_GOOD = "#0ca30c"
_CRITICAL = "#d03b3b"

_CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-secondary: #52514e;
  --muted: #898781;
  --border: rgba(11,11,11,0.10);
  --grid: #e1e0d9;
  --good: #0ca30c;
  --bad: #d03b3b;
  --warning: #fab219;
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
    --grid: #2c2c2a;
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
.wrap { max-width: 920px; margin: 0 auto; }
header.report-header { margin-bottom: 20px; }
header.report-header h1 { font-size: 1.4rem; margin: 0 0 4px; }
header.report-header p { margin: 2px 0; color: var(--ink-secondary); font-size: 0.92rem; }
.muted { color: var(--muted); }
.warning { color: var(--warning); font-size: 0.88rem; }

.combo-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 16px;
  padding: 14px 18px 18px;
}
.combo-title { display: flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 2px; flex-wrap: wrap; }
.rarity-dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
.rank-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 50%;
  background: var(--border); font-size: 0.78rem; font-weight: 700; flex: none;
}
.collection-name { font-weight: 400; color: var(--ink-secondary); }
.ev-badge { margin-left: auto; font-variant-numeric: tabular-nums; font-weight: 700; }
.ev-good { color: var(--good); }
.ev-bad { color: var(--bad); }

.metrics { display: flex; gap: 18px; flex-wrap: wrap; margin: 8px 0 14px; font-size: 0.85rem; }
.metric .label { color: var(--muted); display: block; font-size: 0.75rem; }
.metric .value { font-variant-numeric: tabular-nums; }

h3.section-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); margin: 14px 0 6px; }

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
.data-table a { color: inherit; }

footer { margin-top: 24px; color: var(--muted); font-size: 0.8rem; }
"""


def _rarity_dot(rarity_name: str | None) -> str:
    color = _RARITY_COLOR.get(rarity_name or "", "#888")
    return f'<span class="rarity-dot" style="background:{color}" title="{_esc(rarity_name)}"></span>'


def _age_label(fetched_at: datetime) -> str:
    hours = (now_utc() - fetched_at).total_seconds() / 3600
    if hours < 1:
        return f"{hours * 60:.0f}m ago"
    return f"{hours:.1f}h ago"


def _offers_table(combo: ComboResult) -> str:
    rows = []
    for offer in sorted(combo.offers, key=lambda o: o.price):
        url = f"https://csfloat.com/item/{offer.listing_id}"
        rows.append(
            "<tr>"
            f"<td><a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(offer.listing_id)}</a></td>"
            f"<td>{_esc(offer.wear_name or '—')}</td>"
            f"<td class='num'>{offer.float_value:.6f}</td>"
            f"<td class='num'>{_money(offer.price)}</td>"
            f"<td class='num'>{_age_label(offer.fetched_at)}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        "<th>Listing</th><th>Wear</th><th class='num'>Float</th><th class='num'>Price</th><th class='num'>Fetched</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _outcomes_table(combo: ComboResult) -> str:
    rows = []
    for outcome in combo.outcomes:
        rows.append(
            "<tr>"
            f"<td>{_esc(outcome.skin_name)}</td>"
            f"<td>{_esc(outcome.collection_name)}</td>"
            f"<td class='num'>{_pct(outcome.probability)}</td>"
            f"<td>{_esc(outcome.predicted_wear)}</td>"
            f"<td class='num'>{_money(outcome.net_price)}</td>"
            f"<td class='num'>{_money(outcome.contribution)}</td>"
            "</tr>"
        )
    return (
        "<table class='data-table'><thead><tr>"
        "<th>Possible output</th><th>Collection</th><th class='num'>Chance</th><th>Wear</th>"
        "<th class='num'>Net sell</th><th class='num'>Contribution</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _combo_card(rank: int, combo: ComboResult) -> str:
    skin = combo.input_skin
    name = f"StatTrak™ {skin.name}" if skin.stattrak else skin.name
    ev_class = "ev-good" if combo.expected_value >= 0 else "ev-bad"
    roi = combo.expected_value / combo.real_cost if combo.real_cost > 0 else None

    return f"""<section class="combo-card">
<div class="combo-title">
<span class="rank-badge">{rank}</span>
{_rarity_dot(skin.rarity_name)}{_esc(name)}
<span class="collection-name">— {_esc(skin.collection_name)} [{_esc(skin.rarity_name)}]</span>
<span class="ev-badge {ev_class}">EV {_money(combo.expected_value, signed=True)}</span>
</div>
<div class="metrics">
<div class="metric"><span class="label">Real cost (10 listings)</span><span class="value">{_money(combo.real_cost)}</span></div>
<div class="metric"><span class="label">Expected output value</span><span class="value">{_money(sum(o.contribution for o in combo.outcomes))}</span></div>
<div class="metric"><span class="label">ROI</span><span class="value">{_pct(roi, signed=True)}</span></div>
<div class="metric"><span class="label">Avg. normalized float</span><span class="value">{combo.avg_float:.4f}</span></div>
</div>
<h3 class="section-label">The 10 listings to buy</h3>
{_offers_table(combo)}
<h3 class="section-label">Possible outputs</h3>
{_outcomes_table(combo)}
</section>"""


def render_report(combos: list[ComboResult], *, top_n: int) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    max_age_hours = MAX_OFFER_AGE.total_seconds() / 3600

    cards = "".join(_combo_card(rank, combo) for rank, combo in enumerate(combos, start=1))
    if not combos:
        cards = (
            '<p class="muted">No mono trade-up combo found -- either no skin has '
            f"&ge;10 fresh (&lt;{max_age_hours:.0f}h old) buy-now listings on disk, "
            "or run braindamage.postvalidate (find_contracts.py --postvalidate-csfloat) first.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mono trade-up buy combos</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="report-header">
<h1>Mono trade-up buy combos</h1>
<p>Generated {generated_at} &nbsp;·&nbsp; Top {top_n} mono trade-up combo(s), by real expected value, buyable
right now from CSFloat listings already on disk and younger than {max_age_hours:.0f}h.</p>
<p class="warning">These are point-in-time snapshots, not live availability -- a listing may already be sold or
delisted by the time you act on this. Combos can (and often will) overlap or exclude each other; nothing here
guarantees more than one is simultaneously executable. Negative-EV combos are shown too, if nothing better exists.</p>
</header>
<main>
{cards}
</main>
<footer>Input prices are each listing's real CSFloat ask; output prices are this app's regular price signals, net
of Steam Community Market's real per-sale fee (5% Steam + 10% game fee, computed cent-exact the way Steam itself
computes it -- about {steam_fees.NOMINAL_CUT_OF_GROSS:.0%} of gross for most prices, more for very cheap listings),
same ones contract simulation uses.</footer>
</div>
</body>
</html>"""
