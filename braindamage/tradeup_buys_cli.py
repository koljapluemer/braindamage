"""Terminal entry point: survey the cheapest normal (non-StatTrak) trade-up-input
skins per collectionXtier via SteamApis' CSFloat marketplace data, and write a
self-contained HTML report of the cheapest few per group -- the "what's cheap to buy
right now" counterpart to find_contracts.py's "what contract should I build".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import config, tradeup_buys, tradeup_buys_report
from .cli import _TqdmProgress, _open_in_firefox
from .db import DATA_DIR, SessionLocal, upgrade_database

REPORTS_DIR = DATA_DIR / "reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find-tradeup-buys",
        description=(
            "Fetch current CSFloat marketplace prices (via SteamApis) for every normal skin usable as a "
            "trade-up input, and write a self-contained HTML report of the cheapest few per collection x "
            "rarity tier."
        ),
    )
    parser.add_argument(
        "--top-n-per-group",
        type=int,
        default=tradeup_buys.DEFAULT_TOP_N_PER_GROUP,
        help=(
            "How many cheapest skins to keep per collection x rarity tier "
            f"(default: {tradeup_buys.DEFAULT_TOP_N_PER_GROUP})."
        ),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the report but don't launch Firefox.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_n_per_group <= 0:
        print("--top-n-per-group must be positive", file=sys.stderr)
        return 2
    if not config.STEAMAPIS_KEY:
        print("STEAMAPIS_KEY is not set -- add it to .env (see .env.example)", file=sys.stderr)
        return 2

    upgrade_database()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Surveying cheapest trade-up buy candidates via SteamApis/CSFloat...")
    progress = _TqdmProgress("Pricing candidate skins", "skin")
    with SessionLocal() as session:
        try:
            result = tradeup_buys.survey_cheapest_tradeup_buys(
                session, top_n_per_group=args.top_n_per_group, on_progress=progress
            )
        except Exception as exc:  # noqa: BLE001 -- last line of defense, see below
            # survey_cheapest_tradeup_buys already isolates SteamApis errors into
            # SurveyResult.error and commits partial progress itself -- this is only
            # for something unexpected escaping that. The session may hold uncommitted
            # Skin.last_price updates from skins priced before the crash (the module
            # only commits at the very end of a *successful* pass), so roll those back
            # explicitly rather than leaving a half-open transaction, and write nothing
            # rather than a report built from an inconsistent partial result object.
            session.rollback()
            print(f"Survey failed entirely ({exc}) -- no report written.", file=sys.stderr)
            return 1
        finally:
            progress.close()

        if result.error:
            print(
                f"Survey stopped early after a SteamApis error: {result.error} -- writing a report from "
                f"whatever was fetched first ({result.skins_priced} skin(s) across {len(result.groups)} "
                "group(s)).",
                file=sys.stderr,
            )

        report_html = tradeup_buys_report.render_report(result, top_n_per_group=args.top_n_per_group)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"tradeup-buys-{timestamp}.html"
    report_path.write_text(report_html, encoding="utf-8")
    print(f"Report written to {report_path}")

    if not args.no_open:
        _open_in_firefox(report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
