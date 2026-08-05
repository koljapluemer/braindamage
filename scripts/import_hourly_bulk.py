"""One-off bulk import of the cs2.sh CS2 Historical Item Price Dataset
(data/32052876/, ~52M listing rows + ~17M aggregate rows) into per-skin
aggregated_prices.json signal files.

Throwaway per this project's price-integration philosophy: run by hand once
(`uv run python scripts/import_hourly_bulk.py`), not wired into the app. Uses
DuckDB to read the parquet files and match them out-of-core against a
Skin-derived lookup table, same rationale as this project's old
hourly_price_import.py: tens of millions of rows is too much to push through
the ORM row-by-row.

Only the ask side is imported — trade-up EV only ever needs "price to
acquire"/"price to sell into", both approximated from ask elsewhere in this
app (see braindamage/cs2cap_api.py). Matched rows are buffered per skin and
flushed to that skin's JSON file in bounded batches, so a skin's file gets a
handful of read-modify-write cycles over the whole import, not one per row.
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

from braindamage import pricing, signals
from braindamage.db import SessionLocal
from braindamage.models import Skin
from braindamage.tradeup import WEAR_BUCKETS

DATASET_DIR = Path(__file__).resolve().parent.parent / "data" / "32052876"
LISTING_PARQUET = DATASET_DIR / "cs2_listing_prices_hourly.parquet"
AGGREGATE_PARQUET = DATASET_DIR / "cs2_market_aggregate_hourly.parquet"

LISTING_SOURCES = ("buff", "csfloat", "youpin")
_DUCKDB_MEMORY_LIMIT = "512MB"
_FETCH_BATCH = 100_000
# Flush a skin's buffered new signals to disk once it accumulates this many —
# bounds both memory and the number of read-modify-write cycles per skin file.
_FLUSH_THRESHOLD_PER_SKIN = 5_000

_UNBOUNDED_START = date(2000, 1, 1)
_UNBOUNDED_END = date(2100, 1, 1)


@dataclass
class HourlyImportResult:
    rows_read: int
    rows_matched: int
    rows_written: int


def _open_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{_DUCKDB_MEMORY_LIMIT}'")
    con.execute("PRAGMA threads=2")
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


class _SkinBatchFlusher:
    def __init__(self, threshold: int = _FLUSH_THRESHOLD_PER_SKIN) -> None:
        self._threshold = threshold
        self._buffers: dict[str, list[signals.AggregatedPriceSignal]] = defaultdict(list)
        self._buffered_count = 0
        self.rows_written = 0

    def add(self, skin_id: str, signal: signals.AggregatedPriceSignal) -> None:
        self._buffers[skin_id].append(signal)
        self._buffered_count += 1
        if len(self._buffers[skin_id]) >= self._threshold:
            self._flush_skin(skin_id)

    def _flush_skin(self, skin_id: str) -> None:
        batch = self._buffers.pop(skin_id, [])
        if not batch:
            return
        signals.append_aggregated_prices(skin_id, batch)
        self.rows_written += len(batch)
        self._buffered_count -= len(batch)

    def flush_all(self) -> None:
        for skin_id in list(self._buffers):
            self._flush_skin(skin_id)


def run_listing_import(
    sources: tuple[str, ...] = LISTING_SOURCES,
    start: date = _UNBOUNDED_START,
    end: date = _UNBOUNDED_END,
) -> HourlyImportResult:
    con = _open_duckdb()
    flusher = _SkinBatchFlusher()
    rows_read = 0
    rows_matched = 0
    try:
        _load_skin_variants(con)
        query = (
            _MATCH_CTE.format(where="bucket >= ? AND bucket < ? AND source = ANY(?)")
            + """
            SELECT skin_id, wear_name, source,
                   strftime(bucket, '%Y-%m-%dT%H:%M:%S'),
                   open_ask, high_ask, low_ask, close_ask, ask_volume
            FROM matched
            """
        )
        cur = con.execute(query, [str(LISTING_PARQUET), start, end, list(sources)])

        while True:
            chunk = cur.fetchmany(_FETCH_BATCH)
            if not chunk:
                break
            rows_read += len(chunk)
            for skin_id, wear_name, source, bucket, o, h, low, c, vol in chunk:
                if skin_id is None:
                    continue
                rows_matched += 1
                flusher.add(
                    skin_id,
                    signals.AggregatedPriceSignal(
                        source=source,
                        wear_name=wear_name,
                        bucket=datetime.fromisoformat(bucket),
                        open=o,
                        high=h,
                        low=low,
                        close=c,
                        volume=vol,
                    ),
                )
        flusher.flush_all()
    finally:
        con.close()

    return HourlyImportResult(rows_read=rows_read, rows_matched=rows_matched, rows_written=flusher.rows_written)


def run_aggregate_import(
    start: date = _UNBOUNDED_START,
    end: date = _UNBOUNDED_END,
) -> HourlyImportResult:
    con = _open_duckdb()
    flusher = _SkinBatchFlusher()
    rows_read = 0
    rows_matched = 0
    try:
        _load_skin_variants(con)
        query = (
            _MATCH_CTE.format(where="bucket >= ? AND bucket < ?")
            + """
            SELECT skin_id, wear_name, strftime(bucket, '%Y-%m-%dT%H:%M:%S'), ask, ask_volume
            FROM matched
            """
        )
        cur = con.execute(query, [str(AGGREGATE_PARQUET), start, end])

        while True:
            chunk = cur.fetchmany(_FETCH_BATCH)
            if not chunk:
                break
            rows_read += len(chunk)
            for skin_id, wear_name, bucket, ask, ask_volume in chunk:
                if skin_id is None:
                    continue
                rows_matched += 1
                flusher.add(
                    skin_id,
                    signals.AggregatedPriceSignal(
                        source="cs2sh_aggregate",
                        wear_name=wear_name,
                        bucket=datetime.fromisoformat(bucket),
                        close=ask,
                        volume=ask_volume,
                    ),
                )
        flusher.flush_all()
    finally:
        con.close()

    return HourlyImportResult(rows_read=rows_read, rows_matched=rows_matched, rows_written=flusher.rows_written)


def recalculate_all_last_prices() -> int:
    with SessionLocal() as session:
        skin_rows = list(session.scalars(select(Skin)).all())
        for skin in skin_rows:
            pricing.recalculate_last_price(skin)
        session.commit()
    return len(skin_rows)


if __name__ == "__main__":
    listing_result = run_listing_import()
    print(f"listing: read={listing_result.rows_read} matched={listing_result.rows_matched} written={listing_result.rows_written}")

    aggregate_result = run_aggregate_import()
    print(f"aggregate: read={aggregate_result.rows_read} matched={aggregate_result.rows_matched} written={aggregate_result.rows_written}")

    recalculated = recalculate_all_last_prices()
    print(f"recalculated last_price for {recalculated} skins")
