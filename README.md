# braindamage

A CS2 trade-up contract simulator: browse the skin catalog, fetch prices, design and simulate trade-up contracts (with EV/ROI/CVaR), and batch-survey mono trades. Desktop UI built with PySide6 (Qt for Python).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Running

```bash
uv run main.py
```

(equivalently: activate the project's virtualenv and run `python main.py`)

## Finding contracts from the terminal

The Qt UI is for exploring the catalog and building contracts by hand. To just
find good mono trade-up contracts without opening it, run:

```bash
uv run find_contracts.py --max-input-cost 100
```

This simulates every mono trade-up (10x the single cheapest priced input, per
collection/rarity/StatTrak combo) whose 10 inputs cost at most `--max-input-cost`,
using whatever prices are already on disk -- **it does not fetch new prices**, so
run a fetch first (Maintenance page, or `scripts/import_hourly_bulk.py`) if your
price data is stale. From those, it shortlists the 10 highest EV%, the 10 highest
net win in $, and every contract with a positive 5% CVaR (deduplicated), and
writes a single self-contained HTML report -- collapsed per-contract summaries
(input cost, EV, chance of profit, worst-case loss) that expand into full detail
(input/outcome tables, ideal buying float ranges, an EV-vs-float chart) -- to
`data/reports/`, then opens it in Firefox.

Pass `--no-open` to just write the file without launching a browser.

```bash
uv run find_contracts.py --max-input-cost 100 --no-open
```

### Postvalidating against CSFloat

The report above prices every buying-float range against a wear-tier price
*approximation* (one price per wear bucket, not per exact float). Pass
`--postvalidate-csfloat` to additionally live-check each shortlisted
contract's ranges against [CSFloat](https://csfloat.com)'s real, individually
floated listings:

```bash
uv run find_contracts.py --max-input-cost 100 --postvalidate-csfloat
```

For each range, this checks whether 10 real listings actually exist within
that exact float band right now and what buying them would really cost, and
refreshes every possible output's sell price from CSFloat's live lowest ask
(CSFloat has no buy-order-book endpoint, so this is the closest live
sell-side signal it exposes). Ranges that turn out unexecutable or
negative-EV on those real numbers are dropped from the report; contracts left
with no viable range are dropped entirely.

Requires `CSFLOAT_API_KEY` in `.env` (free — see `.env.example`). This writes
to the same on-disk price signals and DB rows every other price-fetch action
in this app uses, and is slow: multiple CSFloat requests per buying range,
per shortlisted contract.

## Tests

```bash
uv run pytest
```
