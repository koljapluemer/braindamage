"""Terminal entry point: find the best mono trade-up combos actually buyable
right now from fresh CSFloat listings already on disk, and write a
self-contained HTML report of the top few -- the "what can I buy this exact
second" counterpart to find_contracts.py's aggregate-price simulation.

Makes no network calls -- purely reads braindamage.postvalidate's on-disk
MarketOfferSignal data (see braindamage.mono_offer_combos), so run
find_contracts.py --postvalidate-csfloat first if there's nothing fresh yet.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import mono_offer_combos, mono_offer_combos_report
from .cli import _open_in_firefox
from .db import DATA_DIR, SessionLocal, upgrade_database

REPORTS_DIR = DATA_DIR / "reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find-mono-offer-combos",
        description=(
            "Find the best mono trade-up combos buyable right now from CSFloat listings already on disk "
            f"(younger than {mono_offer_combos.MAX_OFFER_AGE.total_seconds() / 3600:.0f}h), and write a "
            "self-contained HTML report of the top few by real expected value."
        ),
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="How many best combos to report, across every input skin (default: 3).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the report but don't launch Firefox.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_n <= 0:
        print("--top-n must be positive", file=sys.stderr)
        return 2

    upgrade_database()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Scanning on-disk CSFloat offers for buyable mono trade-up combos...")
    with SessionLocal() as session:
        combos = mono_offer_combos.find_best_combos(session, top_n=args.top_n)
        print(f"{len(combos)} combo(s) found.")
        html = mono_offer_combos_report.render_report(combos, top_n=args.top_n)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"mono-offer-combos-{timestamp}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"Report written to {report_path}")

    if not args.no_open:
        _open_in_firefox(report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
