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

## Finding cheap trade-up buy candidates

To see what's currently cheap to *buy* for a mono trade-up, rather than
simulate a specific contract, run:

```bash
uv run find_tradeup_buys.py
```

This fetches current CSFloat marketplace prices — via
[SteamApis](https://docs.steamapis.com)'s Market Data API, not CSFloat's own
API — for every normal (non-StatTrak) skin usable as a trade-up input, across
every collection × rarity tier that isn't a dead end (i.e. has a valid output
rarity to trade into), and writes a self-contained HTML report of the
cheapest 3 skins per group. Pass `--top-n-per-group` to change that count, or
`--no-open` to skip launching Firefox.

Fetched prices are written to the same on-disk price signals and DB rows
(`Skin.last_price`) every other price-fetch action in this app uses. If
SteamApis errors partway through (rate limit, no connection, ...), the
survey stops but keeps and reports whatever it already fetched rather than
losing the run.

Requires `STEAMAPIS_KEY` in `.env` (paid — see `.env.example`).

## Finding buyable mono trade-up combos

The reports above price inputs at an aggregate wear-bucket approximation.
Once you've got real per-listing data on disk (via CSFloat postvalidation
above, or the Steam Market scraper below), find the best *actually buyable
right now* mono trade-up combos:

```bash
uv run find_mono_offer_combos.py         # from CSFloat listings (postvalidate_csfloat data)
uv run find_steam_offer_combos.py        # from Steam Community Market listings
```

Both make no network calls of their own — they only read whatever's already
on disk, deduplicate offers younger than 24h, and brute-force the highest
real-expected-value way to pick exactly 10 of them per input skin, writing a
self-contained HTML report of the top 3 (`--top-n` to change that count).
Results can be negative-EV or overlap/mutually exclude each other — this is
about seeing the best options as they stand, not a guaranteed executable plan.

## Steam Market offer scraper

`find_steam_offer_combos.py` above needs Steam Community Market listing data
on disk first. That data comes from a small Firefox extension (`webext/`)
plus a native-messaging host (`braindamage/steam_offers_host.py`) that writes
what it scrapes straight into this app's normal on-disk signal files.

**One-time setup:**

```bash
uv sync   # if you haven't already
scripts/install_native_host.sh
```

This registers the native messaging host with Firefox
(`~/.mozilla/native-messaging-hosts/`). Then load the extension itself: open
`about:debugging` → "This Firefox" → "Load Temporary Add-on…" and select
`webext/manifest.json`.

**Usage:** open any Steam Community Market item listing page (e.g.
`steamcommunity.com/market/listings/730/<item name>`). A sidebar docks
itself to the right edge automatically -- no toolbar click needed -- scrapes
the page, writes one `steam_offers.json` per skin under
`data/skins/<skin_id>/`, and shows a price table for that skin's mono
trade-up (cost to buy 10 at each wear, every possible outcome skin's price,
and the resulting EV per wear). Scroll to load as many listing rows as you
want first (only what's currently rendered gets scraped), then click
**Refresh** in the sidebar to re-scrape.

Click the extension's toolbar button to collapse the sidebar down to a thin
strip (click the strip, or the sidebar's own `»` button, to bring it back);
that state is remembered across page loads.

If you also filter the page to one specific wear (Steam's own "Filters" →
Exterior), the sidebar additionally picks up that wear's buy-order-book
summary ("N requests to buy at $X or lower") and saves it to
`buy_order_summary.json` -- the table then prefers that price (the best
available *sell-side* signal, an instant sale with no listing needed) for
every outcome skin it has one for.

Two things worth knowing:
- **Your Steam account currency must be USD or EUR.** This app assumes USD
  everywhere downstream (pricing, EV math); a EUR scrape is converted to USD
  once, at write time, using a hand-maintained `EUR_USD_RATE` in `.env` (see
  `.env.example` — update it by hand every so often). Any other currency is
  rejected outright rather than silently mixed in.
- Doppler/Gamma Doppler weapon skins (e.g. `Glock-18 | Gamma Doppler`) can't
  be disambiguated by phase from Steam's listing name alone — the host
  reports an "ambiguous" error for those rather than guessing.

## Tests

```bash
uv run pytest
```
