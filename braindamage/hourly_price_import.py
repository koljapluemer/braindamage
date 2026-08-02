"""Importer for the cs2.sh hourly historical price export (data/32052876/).

The source files are tens of millions of rows (52M listing rows, 17M aggregate
rows) — too large to load into pandas/the ORM without risking OOM on a modest
laptop. DuckDB does the heavy lifting instead: it reads the parquet files and
joins them against market_items out-of-core (memory-capped via PRAGMA, spills to
disk rather than blowing up RAM), and results are streamed out of DuckDB in
bounded batches and bulk-inserted into SQLite with raw executemany — never
materializing the full result set, in DuckDB or in Python, at once.

Row values are matched to a MarketItem by market_hash_name, with the source's
`variant_display_name` resolved to MarketItem.phase for the Doppler/Gamma Doppler
family (Ruby/Sapphire/Black Pearl/Emerald/Phase 1-4) — the only variant family the
current schema disambiguates (see docs/skin-mechanics.md). Other variant families
in this dataset (Case Hardened tiers, etc.) have no phase counterpart in
MarketItem, so those rows fall back to the plain (market_hash_name, phase=NULL)
item, same as base rows.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import duckdb
from sqlalchemy import select

from .db import SessionLocal, engine
from .models import MarketItem

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "32052876"
LISTING_PARQUET = DATA_DIR / "cs2_listing_prices_hourly.parquet"
AGGREGATE_PARQUET = DATA_DIR / "cs2_market_aggregate_hourly.parquet"

SOURCES = ("buff", "csfloat", "youpin")

# Caps DuckDB's own working set so it spills to disk instead of growing
# unbounded on a large scan/join — the whole point of using it here.
_DUCKDB_MEMORY_LIMIT = "512MB"

_BATCH_SIZE = 100_000

ProgressCallback = Callable[[int, int], None]


@dataclass
class HourlyImportResult:
    rows_read: int
    rows_matched: int
    rows_inserted: int
    rows_unmatched: int


def _open_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{_DUCKDB_MEMORY_LIMIT}'")
    con.execute("PRAGMA threads=2")
    return con


def _load_market_items(con: duckdb.DuckDBPyConnection) -> None:
    with SessionLocal() as session:
        rows = session.execute(
            select(MarketItem.id, MarketItem.market_hash_name, MarketItem.phase)
        ).all()
    con.execute("CREATE TEMP TABLE market_items (id VARCHAR, market_hash_name VARCHAR, phase VARCHAR)")
    con.executemany("INSERT INTO market_items VALUES (?, ?, ?)", rows)


def get_dataset_bounds(path: Path = LISTING_PARQUET) -> tuple[date, date]:
    """Min/max bucket date in a source file — used to seed the UI's date range."""
    con = _open_duckdb()
    try:
        start, end = con.execute(
            "SELECT MIN(bucket)::DATE, MAX(bucket)::DATE FROM read_parquet(?)", [str(path)]
        ).fetchone()
        return start, end
    finally:
        con.close()


def estimate_listing_rows(sources: list[str], start: date, end: date) -> int:
    con = _open_duckdb()
    try:
        (count,) = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?) WHERE bucket >= ? AND bucket < ? AND source = ANY(?)",
            [str(LISTING_PARQUET), start, end, list(sources)],
        ).fetchone()
        return count
    finally:
        con.close()


def estimate_aggregate_rows(start: date, end: date) -> int:
    con = _open_duckdb()
    try:
        (count,) = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?) WHERE bucket >= ? AND bucket < ?",
            [str(AGGREGATE_PARQUET), start, end],
        ).fetchone()
        return count
    finally:
        con.close()


# Matches a source row to a MarketItem: prefer an exact phase match (Doppler-family
# variants), falling back to the plain item when the source has no phase info or the
# schema doesn't disambiguate that variant family.
_MATCH_CTE = """
    WITH src AS (
        SELECT * FROM read_parquet(?) WHERE {where}
    ), matched AS (
        SELECT COALESCE(exact_phase.id, base.id) AS market_item_id, src.*
        FROM src
        LEFT JOIN market_items exact_phase
            ON exact_phase.market_hash_name = src.market_hash_name
           AND exact_phase.phase = src.variant_display_name
        LEFT JOIN market_items base
            ON base.market_hash_name = src.market_hash_name
           AND base.phase IS NULL
    )
"""


def _bulk_insert(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    row_iter,
) -> tuple[int, int]:
    """Streams already-matched rows from row_iter into `table` in bounded batches.

    Returns (rows_matched, rows_inserted) — rows_inserted can be lower when a run is
    re-imported (INSERT OR IGNORE skips rows already present) or when two source
    variant rows collapse onto the same MarketItem (see module docstring).
    """
    placeholders = ",".join(["?"] * len(columns))
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    cur = conn.cursor()

    rows_matched = 0
    rows_inserted = 0
    batch = []
    for row in row_iter:
        batch.append(row)
        if len(batch) >= _BATCH_SIZE:
            cur.executemany(sql, batch)
            rows_inserted += cur.rowcount if cur.rowcount != -1 else len(batch)
            conn.commit()
            rows_matched += len(batch)
            batch.clear()

    if batch:
        cur.executemany(sql, batch)
        rows_inserted += cur.rowcount if cur.rowcount != -1 else len(batch)
        conn.commit()
        rows_matched += len(batch)

    return rows_matched, rows_inserted


def run_listing_price_import(
    sources: list[str],
    start: date,
    end: date,
    progress_callback: ProgressCallback | None = None,
) -> HourlyImportResult:
    total = estimate_listing_rows(sources, start, end)

    con = _open_duckdb()
    try:
        _load_market_items(con)
        query = _MATCH_CTE.format(where="bucket >= ? AND bucket < ? AND source = ANY(?)") + """
            SELECT market_item_id, source, strftime(bucket, '%Y-%m-%d %H:%M:%S'),
                   open_ask, high_ask, low_ask, close_ask, ask_volume,
                   open_bid, high_bid, low_bid, close_bid, bid_volume, sample_count
            FROM matched
        """
        cur = con.execute(query, [str(LISTING_PARQUET), start, end, list(sources)])

        columns = [
            "market_item_id", "source", "bucket",
            "open_ask", "high_ask", "low_ask", "close_ask", "ask_volume",
            "open_bid", "high_bid", "low_bid", "close_bid", "bid_volume", "sample_count",
        ]

        rows_read = 0
        rows_unmatched = 0

        def matched_rows():
            nonlocal rows_read, rows_unmatched
            while True:
                chunk = cur.fetchmany(_BATCH_SIZE)
                if not chunk:
                    return
                rows_read += len(chunk)
                for row in chunk:
                    if row[0] is None:
                        rows_unmatched += 1
                    else:
                        yield row
                if progress_callback:
                    progress_callback(rows_read, total)

        raw_conn = engine.raw_connection()
        try:
            raw_conn.execute("PRAGMA synchronous=OFF")
            rows_matched, rows_inserted = _bulk_insert(
                raw_conn, "hourly_listing_prices", columns, matched_rows()
            )
        finally:
            raw_conn.close()
    finally:
        con.close()

    return HourlyImportResult(
        rows_read=rows_read,
        rows_matched=rows_matched,
        rows_inserted=rows_inserted,
        rows_unmatched=rows_unmatched,
    )


def run_aggregate_price_import(
    start: date,
    end: date,
    progress_callback: ProgressCallback | None = None,
) -> HourlyImportResult:
    total = estimate_aggregate_rows(start, end)

    con = _open_duckdb()
    try:
        _load_market_items(con)
        query = _MATCH_CTE.format(where="bucket >= ? AND bucket < ?") + """
            SELECT market_item_id, strftime(bucket, '%Y-%m-%d %H:%M:%S'),
                   ask, ask_volume, bid, bid_volume, hourly_volume, total_supply, sample_count
            FROM matched
        """
        cur = con.execute(query, [str(AGGREGATE_PARQUET), start, end])

        columns = [
            "market_item_id", "bucket",
            "ask", "ask_volume", "bid", "bid_volume", "hourly_volume", "total_supply", "sample_count",
        ]

        rows_read = 0
        rows_unmatched = 0

        def matched_rows():
            nonlocal rows_read, rows_unmatched
            while True:
                chunk = cur.fetchmany(_BATCH_SIZE)
                if not chunk:
                    return
                rows_read += len(chunk)
                for row in chunk:
                    if row[0] is None:
                        rows_unmatched += 1
                    else:
                        yield row
                if progress_callback:
                    progress_callback(rows_read, total)

        raw_conn = engine.raw_connection()
        try:
            raw_conn.execute("PRAGMA synchronous=OFF")
            rows_matched, rows_inserted = _bulk_insert(
                raw_conn, "hourly_market_aggregates", columns, matched_rows()
            )
        finally:
            raw_conn.close()
    finally:
        con.close()

    return HourlyImportResult(
        rows_read=rows_read,
        rows_matched=rows_matched,
        rows_inserted=rows_inserted,
        rows_unmatched=rows_unmatched,
    )
