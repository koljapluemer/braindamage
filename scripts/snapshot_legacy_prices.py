"""One-time migration of legacy price histories into compact per-skin snapshots.

Run from the repository root with:
    uv run python scripts/snapshot_legacy_prices.py
"""

import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from braindamage import pricing, signals
from braindamage.db import SessionLocal, upgrade_database
from braindamage.models import Skin


def main() -> None:
    upgrade_database()
    generated_at = signals.now_utc()
    written = 0
    with SessionLocal() as session:
        skins = list(session.scalars(select(Skin).order_by(Skin.id)).all())
        for index, skin in enumerate(skins, start=1):
            latest = pricing.latest_prices_by_wear(skin.id)
            signals.write_legacy_price_snapshot(
                skin.id,
                signals.LegacyPriceSnapshot(
                    generated_at=generated_at,
                    prices_by_wear={
                        wear: signals.LegacyWearPrice(price=price, observed_at=observed_at)
                        for wear, (price, observed_at) in latest.items()
                    },
                ),
            )
            written += 1
            if index % 100 == 0 or index == len(skins):
                print(f"Snapshotted {index}/{len(skins)} skins", flush=True)
    print(f"Wrote {written} legacy price snapshots dated {generated_at.isoformat()}Z")


if __name__ == "__main__":
    main()
