"""Importer for the historic price snapshot in data/Skins_Price.csv.

One-off demo data: a single CSV snapshot with no timestamps, unlike the CS2Cap
API. Values are inserted as PriceObservations with observed_at left unset since
the snapshot date isn't known.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import MarketItem, PriceObservation

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "Skins_Price.csv"

SOURCE = "csv_snapshot"

# CSV column name -> (wear name, stattrak). "Weapon" + "Case" (the skin name, despite
# the header) + wear reproduce the Steam market_hash_name, e.g.
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
    items_not_found: int


def _parse_price(value: str) -> float | None:
    value = value.strip()
    if not value.startswith("$"):
        return None
    return float(value[1:].replace(",", ""))


def _market_hash_name(weapon: str, skin: str, wear: str, stattrak: bool) -> str:
    prefix = "StatTrak™ " if stattrak else ""
    return f"{prefix}{weapon} | {skin} ({wear})"


def _import_row(session: Session, row: dict) -> tuple[int, int]:
    observations = 0
    items_not_found = 0

    for column, stattrak in _WEAR_COLUMNS:
        price = _parse_price(row[column])
        if price is None:
            continue

        wear = column.removeprefix("StatTrak ")
        market_hash_name = _market_hash_name(row["Weapon"], row["Case"], wear, stattrak)
        market_items = session.scalars(
            select(MarketItem).where(MarketItem.market_hash_name == market_hash_name)
        ).all()
        if not market_items:
            items_not_found += 1
            continue

        for market_item in market_items:
            session.add(
                PriceObservation(
                    market_item_id=market_item.id,
                    source=SOURCE,
                    side="ask",
                    currency="USD",
                    price=price,
                    raw={"weapon": row["Weapon"], "skin": row["Case"], "column": column},
                )
            )
            observations += 1

    return observations, items_not_found


def run_csv_price_import(csv_path: Path | str = DEFAULT_CSV_PATH) -> CsvImportResult:
    rows_read = 0
    observations = 0
    items_not_found = 0

    with SessionLocal() as session, open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows_read += 1
            row_observations, row_items_not_found = _import_row(session, row)
            observations += row_observations
            items_not_found += row_items_not_found
        session.commit()

    return CsvImportResult(
        rows_read=rows_read, observations=observations, items_not_found=items_not_found
    )
