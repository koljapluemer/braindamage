"""One-off bulk import of the cs2.sh CS2 Historical Item Price Dataset
(data/32052876/, ~52M listing rows + ~17M aggregate rows) into per-skin
aggregated_prices.json signal files.

Throwaway per this project's price-integration philosophy: run by hand once
(`uv run python scripts/import_hourly_bulk.py`), not wired into the app. This
dataset is only a test bed for trade-up EV, which needs one current "price to
acquire"/"price to sell into" per skin x wear x normal/StatTrak/Souvenir, not
a price history — so rather than pulling tens of millions of rows through
Python, DuckDB does the reduction to "latest row per (skin, wear, source)"
out-of-core via a single hash aggregation (`arg_max`), and only that tiny
result (bounded by skin x wear x source cardinality, not by row count) ever
reaches Python. Same join rationale as this project's old
hourly_price_import.py: matching tens of millions of rows is too much for the
ORM row-by-row.

Only the ask side is imported — trade-up EV only ever needs "price to
acquire"/"price to sell into", both approximated from ask elsewhere in this
app (see braindamage/cs2cap_api.py).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
from sqlalchemy import select
from tqdm import tqdm

from braindamage import pricing, signals
from braindamage.db import SessionLocal
from braindamage.models import Skin
from braindamage.tradeup import WEAR_BUCKETS

DATASET_DIR = Path(__file__).resolve().parent.parent / "data" / "32052876"
LISTING_PARQUET = DATASET_DIR / "cs2_listing_prices_hourly.parquet"
AGGREGATE_PARQUET = DATASET_DIR / "cs2_market_aggregate_hourly.parquet"

LISTING_SOURCES = ("buff", "csfloat", "youpin")
_DUCKDB_MEMORY_LIMIT = "512MB"

_UNBOUNDED_START = date(2000, 1, 1)
_UNBOUNDED_END = date(2100, 1, 1)


@dataclass
class HourlyImportResult:
    rows_matched: int
    skins_written: int


def _open_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{_DUCKDB_MEMORY_LIMIT}'")
    con.execute("PRAGMA threads=2")
    con.execute("PRAGMA enable_progress_bar")
    return con


def _load_skin_variants(con: duckdb.DuckDBPyConnection) -> None:
    """Ephemeral (skin_id, market_hash_name, wear_name, phase) lookup for the
    DuckDB join — the same wear-cartesian-product braindamage.cs2cap_api builds
    market_hash_names from, materialized here instead of one API call at a
    time."""
    with SessionLocal() as session:
        skin_rows = list(session.scalars(select(Skin)).all())

    rows = []
    for skin in skin_rows:
        if skin.stattrak:
            prefix = "StatTrak™ "
        elif skin.souvenir:
            prefix = "Souvenir "
        else:
            prefix = ""
        for wear_name, _lo, _hi in WEAR_BUCKETS:
            rows.append((skin.id, f"{prefix}{skin.name} ({wear_name})", wear_name, skin.phase))

    con.execute(
        "CREATE TEMP TABLE skin_variants "
        "(skin_id VARCHAR, market_hash_name VARCHAR, wear_name VARCHAR, phase VARCHAR)"
    )
    con.executemany("INSERT INTO skin_variants VALUES (?, ?, ?, ?)", rows)


# Matches a source row to a skin variant: prefer an exact phase match
# (Doppler-family variants), falling back to the plain (phase IS NULL) variant
# when the source has no phase info or this variant family isn't disambiguated.
_MATCH_CTE = """
    WITH src AS (
        SELECT * FROM read_parquet(?) WHERE {where}
    ), matched AS (
        SELECT
            COALESCE(exact_phase.skin_id, base.skin_id) AS skin_id,
            COALESCE(exact_phase.wear_name, base.wear_name) AS wear_name,
            src.*
        FROM src
        LEFT JOIN skin_variants exact_phase
            ON exact_phase.market_hash_name = src.market_hash_name
           AND exact_phase.phase = src.variant_display_name
        LEFT JOIN skin_variants base
            ON base.market_hash_name = src.market_hash_name
           AND base.phase IS NULL
    )
"""


def _write_latest_signals(rows: list[tuple]) -> int:
    """rows: (skin_id, wear_name, source, bucket_iso, open, high, low, close, volume),
    one row per (skin_id, wear_name, source) already reduced to its latest bucket by
    DuckDB. Grouped by skin so each skin's file gets a single read-modify-write."""
    by_skin: dict[str, list[signals.AggregatedPriceSignal]] = defaultdict(list)
    for skin_id, wear_name, source, bucket, o, h, low, c, vol in rows:
        if skin_id is None:
            continue
        by_skin[skin_id].append(
            signals.AggregatedPriceSignal(
                source=source,
                wear_name=wear_name,
                bucket=datetime.fromisoformat(bucket),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=vol,
            )
        )

    for skin_id, skin_signals in tqdm(by_skin.items(), desc="writing skin signal files", unit="skin"):
        signals.append_aggregated_prices(skin_id, skin_signals)

    return len(by_skin)


def run_listing_import(
    sources: tuple[str, ...] = LISTING_SOURCES,
    start: date = _UNBOUNDED_START,
    end: date = _UNBOUNDED_END,
) -> HourlyImportResult:
    con = _open_duckdb()
    try:
        _load_skin_variants(con)
        query = (
            _MATCH_CTE.format(where="bucket >= ? AND bucket < ? AND source = ANY(?)")
            + """
            SELECT skin_id, wear_name, source,
                   strftime(max(bucket), '%Y-%m-%dT%H:%M:%S'),
                   arg_max(open_ask, bucket), arg_max(high_ask, bucket),
                   arg_max(low_ask, bucket), arg_max(close_ask, bucket),
                   arg_max(ask_volume, bucket)
            FROM matched
            WHERE skin_id IS NOT NULL
            GROUP BY skin_id, wear_name, source
            """
        )
        rows = con.execute(query, [str(LISTING_PARQUET), start, end, list(sources)]).fetchall()
    finally:
        con.close()

    skins_written = _write_latest_signals(rows)
    return HourlyImportResult(rows_matched=len(rows), skins_written=skins_written)


def run_aggregate_import(
    start: date = _UNBOUNDED_START,
    end: date = _UNBOUNDED_END,
) -> HourlyImportResult:
    con = _open_duckdb()
    try:
        _load_skin_variants(con)
        query = (
            _MATCH_CTE.format(where="bucket >= ? AND bucket < ?")
            + """
            SELECT skin_id, wear_name, 'cs2sh_aggregate',
                   strftime(max(bucket), '%Y-%m-%dT%H:%M:%S'),
                   NULL, NULL, NULL,
                   arg_max(ask, bucket),
                   arg_max(ask_volume, bucket)
            FROM matched
            WHERE skin_id IS NOT NULL
            GROUP BY skin_id, wear_name
            """
        )
        rows = con.execute(query, [str(AGGREGATE_PARQUET), start, end]).fetchall()
    finally:
        con.close()

    skins_written = _write_latest_signals(rows)
    return HourlyImportResult(rows_matched=len(rows), skins_written=skins_written)


def recalculate_all_last_prices() -> int:
    with SessionLocal() as session:
        skin_rows = list(session.scalars(select(Skin)).all())
        for skin in skin_rows:
            pricing.recalculate_last_price(skin)
        session.commit()
    return len(skin_rows)


if __name__ == "__main__":
    listing_result = run_listing_import()
    print(f"listing: matched={listing_result.rows_matched} skins_written={listing_result.skins_written}")

    aggregate_result = run_aggregate_import()
    print(f"aggregate: matched={aggregate_result.rows_matched} skins_written={aggregate_result.skins_written}")

    recalculated = recalculate_all_last_prices()
    print(f"recalculated last_price for {recalculated} skins")
