"""Static HTML report of braindamage.tradeup_buys' survey results -- the top few
cheapest normal (non-StatTrak) skins per collectionXtier trade-up input group, priced
via SteamApis' CSFloat marketplace data. Same "single self-contained file, no JS, meant
to be opened once and read" shape as braindamage.report's contract report, and reuses
its escaping/formatting helpers so numbers render identically across both reports.
"""

from __future__ import annotations

from datetime import datetime

from .report import _esc, _money
from .tradeup import RARITY_LADDER
from .tradeup_buys import GroupCandidates, SurveyResult

_RARITY_COLOR = dict(RARITY_LADDER)
_RARITY_RANK = {name: rank for rank, (name, _color) in enumerate(RARITY_LADDER)}

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
.wrap { max-width: 900px; margin: 0 auto; }
header.report-header { margin-bottom: 20px; }
header.report-header h1 { font-size: 1.4rem; margin: 0 0 4px; }
header.report-header p { margin: 2px 0; color: var(--ink-secondary); font-size: 0.92rem; }
.muted { color: var(--muted); }
.warning { color: var(--warning); font-size: 0.88rem; }

.group-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 10px;
  padding: 10px 14px 14px;
}
.group-title { display: flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 6px; }
.rarity-dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
.collection-name { font-weight: 400; color: var(--ink-secondary); }

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

footer { margin-top: 24px; color: var(--muted); font-size: 0.8rem; }
"""


def _rarity_dot(rarity_name: str) -> str:
    color = _RARITY_COLOR.get(rarity_name, "#888")
    return f'<span class="rarity-dot" style="background:{color}" title="{_esc(rarity_name)}"></span>'


def _group_card(group: GroupCandidates) -> str:
    rows = []
    for rank, candidate in enumerate(group.candidates, start=1):
        skin = candidate.skin
        name = f'StatTrak™ {skin.name}' if skin.stattrak else skin.name
        rows.append(
            "<tr>"
            f"<td class='num'>{rank}</td>"
            f"<td>{_esc(name)}</td>"
            f"<td>{_esc(candidate.wear_name)}</td>"
            f"<td class='num'>{_money(candidate.price)}</td>"
            "</tr>"
        )
    table = (
        "<table class='data-table'><thead><tr>"
        "<th class='num'>#</th><th>Skin</th><th>Wear</th><th class='num'>Price</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return (
        '<section class="group-card">'
        f'<div class="group-title">{_rarity_dot(group.rarity_name)}{_esc(group.rarity_name)}'
        f'<span class="collection-name">— {_esc(group.collection_name)}</span></div>'
        f"{table}"
        "</section>"
    )


def render_report(result: SurveyResult, *, top_n_per_group: int) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ordered_groups = sorted(
        result.groups,
        key=lambda g: (_RARITY_RANK.get(g.rarity_name, len(RARITY_LADDER)), g.collection_name),
    )
    cards = "".join(_group_card(g) for g in ordered_groups)
    if not ordered_groups:
        cards = '<p class="muted">No priced candidates -- check STEAMAPIS_KEY and that SteamApis has CSFloat data for this catalog.</p>'

    error_note = (
        f'<p class="warning">Survey stopped early after a SteamApis error: {_esc(result.error)} '
        "-- results below reflect only what was fetched before that point.</p>"
        if result.error
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trade-up buy candidates</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="report-header">
<h1>Trade-up buy candidates</h1>
<p>Generated {generated_at} &nbsp;·&nbsp; Top {top_n_per_group} cheapest normal (non-StatTrak) skin(s) per
collection × rarity tier that's usable as a trade-up input, priced via SteamApis' CSFloat marketplace data.</p>
<p>{len(ordered_groups)} group(s) priced, {result.skins_priced} skin(s) quoted, {result.requests_made} SteamApis
request(s) made.</p>
{error_note}</header>
<main>
{cards}
</main>
<footer>Prices are live SteamApis/CSFloat reads as of generation time, and were also written to this app's
regular price signals (same ones contract simulation uses) and to each skin's last_price.</footer>
</div>
</body>
</html>"""
