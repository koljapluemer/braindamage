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

from . import config, mono_trades, postvalidate, report
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

    def close(self) -> None:
        """For a caller that may stop early (e.g. postvalidate_contracts'
        circuit breakers) and so never hits the done >= total close above --
        safe to call even if the bar already closed itself or was never
        created at all."""
        if self._bar is not None:
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
    parser.add_argument(
        "--postvalidate-csfloat",
        action="store_true",
        help=(
            "After shortlisting, check each contract's buying-float ranges against CSFloat's live "
            "floated listings: real cost (and whether it's even possible) to buy the 10 inputs in "
            "that exact float range right now, plus a live lowest-ask refresh for every possible "
            "output. Ranges that are unexecutable or negative-EV on real numbers are dropped from "
            "the report; contracts left with no viable range are dropped entirely. Writes to the "
            "same on-disk price signals and DB rows every other price-fetch action uses. Requires "
            "CSFLOAT_API_KEY in .env. Slow: multiple CSFloat requests per buying range, per "
            "shortlisted contract."
        ),
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
    if args.postvalidate_csfloat and not config.CSFLOAT_API_KEY:
        print("--postvalidate-csfloat requires CSFLOAT_API_KEY in .env", file=sys.stderr)
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

        postvalidated = False
        if args.postvalidate_csfloat:
            print(f"Postvalidating {len(selection.contracts)} contract(s) against CSFloat...")
            pre_postvalidation_selection = selection
            progress = _TqdmProgress("Postvalidating", "contract")
            try:
                results = postvalidate.postvalidate_contracts(
                    session, selection.contracts, on_progress=progress
                )
                errored = [r for r in results if r.error is not None]
                if errored:
                    print(
                        f"{len(errored)}/{len(results)} contract(s) hit an error partway through "
                        "postvalidation (most likely CSFloat rate limiting) -- whatever ranges they'd "
                        "already checked are kept; unchecked ranges are treated as unconfirmed.",
                        file=sys.stderr,
                    )
                if len(results) < len(selection.contracts):
                    print(
                        f"Only {len(results)}/{len(selection.contracts)} shortlisted contract(s) were "
                        "attempted -- a circuit breaker stopped the rest early (see message above); "
                        "the untouched ones are excluded below rather than shown unconfirmed.",
                        file=sys.stderr,
                    )
                selection = report.filter_postvalidated(selection, session)
                postvalidated = True
                print(f"{len(selection.contracts)} contract(s) remain after postvalidation.")
            except Exception as exc:  # noqa: BLE001 -- last line of defense, see below
                # Whatever went wrong here, the simulation work above (which can take
                # many minutes) must not be thrown away -- fall back to the
                # pre-postvalidation selection and still write a report from it,
                # rather than crash with nothing written at all.
                print(
                    f"Postvalidation failed entirely ({exc}) -- writing the report without it instead of "
                    "discarding the simulation above.",
                    file=sys.stderr,
                )
                selection = pre_postvalidation_selection
            finally:
                progress.close()

        html = report.render_report(
            selection, session, max_input_cost=args.max_input_cost, postvalidated=postvalidated
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"mono-trades-{timestamp}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"Report written to {report_path}")

    if not args.no_open:
        _open_in_firefox(report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
