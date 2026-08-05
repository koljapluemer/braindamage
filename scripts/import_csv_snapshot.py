"""One-off importer for the historic CSV price snapshot at data/Skins_Price.csv.

No timestamps in the source — every row is stamped with the time this script
ran (`fetched_at`), `observed_at` is left unset. Matches each column to a Skin
by reconstructing "<Weapon> | <Case>" and StatTrak state (the CSV has no
souvenir columns, so souvenir skins are never matched here). Not part of the
running app — run by hand once (`uv run python scripts/import_csv_snapshot.py`).
"""

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from braindamage import pricing, signals
from braindamage.db import SessionLocal
from braindamage.models import Skin

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "Skins_Price.csv"
SOURCE = "csv_snapshot"

# CSV column name -> (wear name, stattrak). "Weapon" + "Case" (the skin name,
# despite the header) + wear reproduce the Steam market_hash_name, e.g.
# "StatTrak™ CZ75-Auto | Victoria (Factory New)".
_WEAR_COLUMNS = [
    ("Factory New", False),
    ("Minimal Wear", False),
    ("Field-Tested", False),
    ("Well-Worn", False),
    ("Battle-Scarred", False),
    ("StatTrak Factory New", True),
    ("StatTrak Minimal Wear", True),
    ("StatTrak Field-Tested", True),
    ("StatTrak Well-Worn", True),
    ("StatTrak Battle-Scarred", True),
]


@dataclass
class CsvImportResult:
    rows_read: int
    observations: int
    skins_not_found: int


def _parse_price(value: str) -> float | None:
    value = value.strip()
    if not value.startswith("$"):
        return None
    return float(value[1:].replace(",", ""))


def run_import(csv_path: Path = CSV_PATH) -> CsvImportResult:
    rows_read = 0
    skins_not_found = 0
    fetched_at = signals.now_utc()
    new_by_skin: dict[str, list[signals.PriceObservationSignal]] = {}

    with SessionLocal() as session, open(csv_path, newline="", encoding="utf-8") as f:
        skin_lookup = {
            (skin.name, skin.stattrak): skin
            for skin in session.scalars(select(Skin).where(Skin.souvenir.is_(False)))
        }

        for row in csv.DictReader(f):
            rows_read += 1
            base_name = f"{row['Weapon']} | {row['Case']}"

            for column, stattrak in _WEAR_COLUMNS:
                price = _parse_price(row[column])
                if price is None:
                    continue
                wear = column.removeprefix("StatTrak ")
                skin = skin_lookup.get((base_name, stattrak))
                if skin is None:
                    skins_not_found += 1
                    continue
                new_by_skin.setdefault(skin.id, []).append(
                    signals.PriceObservationSignal(
                        source=SOURCE,
                        wear_name=wear,
                        price=price,
                        fetched_at=fetched_at,
                        raw={"weapon": row["Weapon"], "skin": row["Case"], "column": column},
                    )
                )

        observations = 0
        for skin_id, new_observations in new_by_skin.items():
            signals.append_price_observations(skin_id, new_observations)
            observations += len(new_observations)
            skin = session.get(Skin, skin_id)
            pricing.recalculate_last_price(skin)
        session.commit()

    return CsvImportResult(rows_read=rows_read, observations=observations, skins_not_found=skins_not_found)


if __name__ == "__main__":
    result = run_import()
    print(f"rows read: {result.rows_read}")
    print(f"observations written: {result.observations}")
    print(f"skins not found: {result.skins_not_found}")
