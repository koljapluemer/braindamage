# Hourly price schema expansion — cs2.sh historical import

Notes on the third price integration (`braindamage/hourly_price_import.py`,
wired into `pages/1_Import.py`), added for `data/32052876/` — the cs2.sh CS2
Historical Item Price Dataset (25 days, hourly buckets, BUFF/CSFloat/Youpin +
a cross-marketplace aggregate). Recorded here because the design departs from
the existing price import pattern in ways that aren't obvious from the code
alone.

## Why a new table instead of `PriceObservation`

The two source files are 52M (listing) and 17M (aggregate) rows — several
orders of magnitude bigger than the CS2Cap/CSV imports. `PriceObservation`
stores a JSON `raw` blob per row by design (see its docstring), which is the
right trade-off for a source fetched a few thousand rows at a time, but at
tens of millions of rows the JSON overhead alone would multiply the on-disk
footprint several times over — directly working against the goal of running
this app on a modest laptop.

Instead, two new typed tables mirror the source files column-for-column:
`hourly_listing_prices` (per market item x source x hour, OHLC ask/bid) and
`hourly_market_aggregates` (per market item x hour, cross-marketplace). Both
use a composite primary key (`market_item_id, source, bucket` / `market_item_id,
bucket`) instead of a surrogate id — one less column, one less index, and it
doubles as the natural dedup key.

## Why DuckDB

Reading either file into pandas or building `PriceObservation` ORM objects
row-by-row isn't viable at this scale on constrained hardware. DuckDB reads
the parquet files directly and does the join against `market_items`
out-of-core: `PRAGMA memory_limit='512MB'` caps its own working set so it
spills to disk rather than growing unbounded, and results are pulled out via
`fetchmany()` in bounded batches (never materializing the full 52M/17M-row
result in DuckDB or in Python at once) and bulk-inserted into SQLite with raw
`sqlite3` `executemany` — bypassing per-row ORM overhead entirely. A full
unfiltered scan of the 52M-row listing file was timed at ~3.5 minutes with
peak Python RSS under 200MB; the Import page's date/source filters exist so a
typical click is much smaller than that.

`INSERT OR IGNORE` plus the composite primary key makes an import safe to
re-run: an interrupted run (closed tab, sleeping laptop) can just be retried
and already-written rows are skipped rather than raising or duplicating.

Timestamps are cast to plain `'%Y-%m-%d %H:%M:%S'` strings inside the DuckDB
query before they ever reach Python — fetching DuckDB's native
timezone-aware `TIMESTAMP WITH TIME ZONE` value requires `pytz`, which isn't
a project dependency, and a plain string is what SQLAlchemy's SQLite
`DateTime` column expects on the way back in anyway.

## Matching source rows to a `MarketItem`

`market_items` (~21k rows) is loaded into a DuckDB temp table so the match
happens inside the same query as the parquet scan, not as a Python-side
per-row lookup. Matching is by `market_hash_name`, with the source's
`variant_display_name` resolved against `MarketItem.phase` for the
Doppler/Gamma Doppler family — the values line up exactly (`Ruby`, `Sapphire`,
`Black Pearl`, `Emerald`, `Phase 1`-`Phase 4`), since that's the same phase
vocabulary `csgo_api._derive_phase` produces (see `docs/skin-mechanics.md`,
"Market items & pricing").

The source dataset also carries other variant families `MarketItem.phase`
doesn't disambiguate — Case Hardened tiers (`t1`-`t4`) and unlabeled ordinal
variants (`1st`-`10th`, presumably Fade-style gradient tiers). Rows for those
fall back to the plain `(market_hash_name, phase IS NULL)` item, same as base
rows — multiple tiers of the same skin collapse onto one `MarketItem`, which
is a real information loss but consistent with what the rest of the schema
already does for anything it doesn't track.

Rows that don't match any `MarketItem` at all are common and expected: the
cs2.sh dataset is market-wide (all Steam item types), while `market_items` is
sourced from bymykel's skins-only catalog. Sampling unmatched names during
testing confirmed they're agents, stickers, patches, and souvenir packages —
not a matching bug. `HourlyImportResult.rows_unmatched` reports the count so
this is visible in the UI rather than silently dropped.
