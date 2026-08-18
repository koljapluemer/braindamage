"""Terminal entry point: find the best mono trade-up combos actually buyable
right now from fresh Steam Community Market listings already on disk, and
write a self-contained HTML report of the top few -- the Steam-market
counterpart to find_mono_offer_combos.py (which does the same against
CSFloat listings).

Makes no network calls -- purely reads braindamage.steam_offers_host's
on-disk SteamOfferSignal data (see braindamage.steam_offer_combos), so use
the companion Firefox extension to fetch offers from an open Steam Market
listing page first if there's nothing fresh yet.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import steam_offer_combos, steam_offer_combos_report
from .cli import _open_in_firefox
from .db import DATA_DIR, SessionLocal, upgrade_database

REPORTS_DIR = DATA_DIR / "reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find-steam-offer-combos",
        description=(
            "Find the best mono trade-up combos buyable right now from Steam Community Market listings "
            f"already on disk (younger than {steam_offer_combos.MAX_OFFER_AGE.total_seconds() / 3600:.0f}h), "
            "and write a self-contained HTML report of the top few by real expected value."
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

    print("Scanning on-disk Steam Market offers for buyable mono trade-up combos...")
    with SessionLocal() as session:
        combos = steam_offer_combos.find_best_combos(session, top_n=args.top_n)
        print(f"{len(combos)} combo(s) found.")
        html = steam_offer_combos_report.render_report(combos, top_n=args.top_n)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"steam-offer-combos-{timestamp}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"Report written to {report_path}")

    if not args.no_open:
        _open_in_firefox(report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
