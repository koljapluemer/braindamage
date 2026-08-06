"""Terminal entry point: find good mono trade-up contracts under a cost cap and
write a static HTML report, without opening the Qt app -- the "core flow"
(simulate mono trades, look at the good ones) the app's Maintenance page button
and Contracts detail dialog otherwise require a GUI session for.

Does not fetch prices -- it simulates purely against whatever price signals are
already on disk (see braindamage.pricing), same as the Maintenance page's
"Recalculate (no price fetch)" action.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from . import mono_trades, report
from .db import DATA_DIR, SessionLocal, upgrade_database

REPORTS_DIR = DATA_DIR / "reports"

# generate_mono_trades caps its result at top_n after sorting by (net $) EV --
# the report needs every priced-under-budget mono trade so it can independently
# rank by EV%, by net $, and by CVaR, so this is passed as an effectively
# unbounded cap (there's at most one contract per collection x rarity x
# StatTrak combo, always far below this).
_NO_CAP = 1_000_000


class _TqdmProgress:
    """Adapts a `generate_mono_trades` (done, total) progress callback to a
    tqdm bar, created lazily on the first call once `total` is actually known."""

    def __init__(self, desc: str, unit: str) -> None:
        self._desc = desc
        self._unit = unit
        self._bar: tqdm | None = None
        self._done = 0

    def __call__(self, done: int, total: int) -> None:
        if self._bar is None:
            self._bar = tqdm(total=total, desc=self._desc, unit=self._unit)
        self._bar.update(done - self._done)
        self._done = done
        if done >= total:
            self._bar.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find-contracts",
        description=(
            "Simulate mono trade-up contracts under a max input cost, from prices already on disk, "
            "and write a single self-contained HTML report of the best ones."
        ),
    )
    parser.add_argument(
        "--max-input-cost",
        type=float,
        required=True,
        help="Only consider mono trades whose 10 inputs cost at most this much (USD).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the report but don't launch Firefox.",
    )
    return parser


def _open_in_firefox(path: Path) -> None:
    try:
        subprocess.Popen(["firefox", str(path.resolve())])
    except FileNotFoundError:
        print(f"Firefox not found on PATH -- open the report manually: {path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_input_cost <= 0:
        print("--max-input-cost must be positive", file=sys.stderr)
        return 2

    upgrade_database()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Simulating mono trades with input cost <= ${args.max_input_cost:,.2f}...")
    with SessionLocal() as session:
        rows = mono_trades.generate_mono_trades(
            session,
            max_input_cost=args.max_input_cost,
            top_n=_NO_CAP,
            on_collection_progress=_TqdmProgress("Scanning collections", "collection"),
            on_upsert_progress=_TqdmProgress("Simulating shortlisted contracts", "contract"),
        )
        print(f"{len(rows)} mono trade contract(s) within budget.")

        selection = report.select_contracts(rows)
        print(
            f"Shortlisted {len(selection.contracts)}: top {selection.top_ev_pct_count} by EV%, "
            f"top {selection.top_net_win_count} by net win $, {selection.positive_cvar_count} with positive CVaR."
        )
        html = report.render_report(selection, session, max_input_cost=args.max_input_cost)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"mono-trades-{timestamp}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"Report written to {report_path}")

    if not args.no_open:
        _open_in_firefox(report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
