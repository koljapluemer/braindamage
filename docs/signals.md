# Signals: per-skin JSON price/event data

Replaces the old `PriceObservation` / `HourlyListingPrice` / `HourlyMarketAggregate`
SQL tables. Only `Skin` and `Contract` are first-class SQL entities (see
`braindamage/models.py`); everything else that used to need a new table for a new
data shape now lives as strictly-typed JSON files instead, so a new signal kind is
a new file and a new Pydantic model — never a migration.

## Layout

```
data/skins/<skin_id>/
    price_observations.json   # point-in-time price readings
    aggregated_prices.json    # OHLC-style hourly buckets (bulk historical import)
    market_offers.json         # individual live floated listings (CSFloat postvalidation)
    events.json                # reserved for future signals (Valve announcements,
                                # streamer callouts, etc.) — no writer exists yet
```

One file per **kind**, not one combined blob per skin: each write stays small, and
each kind is independently typed (see the Pydantic models in
`braindamage/signals.py`: `PriceObservationSignal`, `AggregatedPriceSignal`,
`MarketOfferSignal`, `SkinEvent`). Entries are append-only — never edited or removed.

`market_offers.json` is written by `braindamage.postvalidate` (see
docs/skin-mechanics.md or the module docstring) when it checks a trade-up's
buying-float range against CSFloat's real, individually-floated listings — one
entry per listing observed, kept as a historical record for later
float-vs-price correlation analysis. Unlike the other two price-signal kinds,
it is never read back by `braindamage.pricing` — a per-listing float snapshot
doesn't fit "latest price for this wear" resolution, it's audit trail only.
CSFloat's *lowest ask* per wear bucket, by contrast, is written as an ordinary
`PriceObservationSignal(source="csfloat", ...)` and does flow through the
normal pricing machinery.

## Why `Skin` doesn't split by wear

A `Skin` row is one weapon pattern x StatTrak/Normal/Souvenir variant — it does
*not* split further by wear condition (Factory New .. Battle-Scarred). Wear-level
price detail lives inside a skin's signal files instead (`wear_name` on each
`PriceObservationSignal`/`AggregatedPriceSignal`), so float-vs-price correlation
analysis is still possible from the raw signal data even though SQL only carries
one row per tradeable listing.

## How `Skin.last_price` gets calculated

`Skin.last_price`, `last_price_recalculated_at`, and
`last_price_calculation_data_point_recency` are *calculated* fields — never
written by hand. `braindamage.pricing.recalculate_last_price(skin)` reads that
skin's signal files, takes the single latest observation across every wear
combined, and writes:

- `last_price` — that observation's price
- `last_price_calculation_data_point_recency` — that observation's own timestamp
- `last_price_recalculated_at` — when the calculation ran (can differ from the
  above if recalculation runs some time after the data was collected)

This is called after every signal write: `braindamage.cs2cap_api.run_price_import`
(the maintenance screen's live fetch) and the throwaway bulk-import scripts (see
`scripts/`).

For anything that needs a specific wear's price (the trade-up simulator, e.g. "the
predicted output float maps to Battle-Scarred, what's that worth"), use
`braindamage.pricing.latest_price_for_wear(skin_id, wear_name)` instead — it
doesn't use the cached `last_price` at all, it reads the signals fresh, filtered
to that wear.

## One-time integrations are throwaway scripts

Catalog import (bymykel's CSGO-API), the historic CSV price snapshot, and the
cs2.sh bulk hourly dataset are all infrequent, one-off operations — they live in
`scripts/`, run by hand, and are not wired into the Textual app. Only the ongoing,
repeatable action (fetch current prices for one skin via CS2Cap) is a UI action,
on the Maintenance screen. See each script's module docstring for specifics.
