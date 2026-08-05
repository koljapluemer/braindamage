# Skin mechanics — research findings

Findings from investigating the CS2 skin data model and the Trade Up Contract,
gathered while scoping a future trade-up simulator. Verified against primary
sources (game patch notes, wiki, direct data queries), not blog summaries.

## Data sources

- `https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json`
  — one row per skin *pattern* (weapon + finish); the shared metadata (name,
  weapon, rarity, collection, float range) for every variant of that pattern.
- `https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins_not_grouped.json`
  — one row per actual tradeable variant (wear tier × normal/StatTrak/Souvenir).
  Each row has a `skin_id` field that maps 1:1 back to the `id` of its base
  pattern in `skins.json`. `scripts/import_catalog.py` groups this down to
  (pattern, StatTrak, Souvenir, phase) — dropping wear — to build one `Skin`
  row per tradeable listing; see docs/signals.md for why wear itself isn't a
  separate row.
- `https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/collections.json`
  — collections.

Both files mirror Valve's own item data, not a third party's interpretation
of it — treat them as authoritative for "what exists," unlike marketplace
blogs.

## Quality variants: Normal / StatTrak / Souvenir

The `stattrak` / `souvenir` booleans on a `skins.json` row mean **"a
StatTrak/Souvenir version of this skin exists,"** not "this row is that
version." Confirmed by cross-referencing all 2126 base skins against
`skins_not_grouped.json`, grouped by `skin_id`:

| Variants present            | Count |
|------------------------------|------:|
| normal + Souvenir             |   757 |
| normal + Souvenir + StatTrak  |   698 |
| normal + StatTrak             |   576 |
| normal only                   |    94 |
| **Souvenir only**             | **1** |

Takeaways:

- **StatTrak is strictly additive.** Every skin with a StatTrak version also
  has a normal version — zero exceptions in the current dataset.
- **Souvenir is *almost* always additive**, with one known exception:
  `MP5-SD | Lab Rats` (`skin-e73d6e7e9004`) exists only as Souvenir, capped at
  Field-Tested wear, with no normal or StatTrak version. Since `Skin` rows are
  now per-variant (not per-pattern), this just means no normal-variant `Skin`
  row gets created for it — no separate capability flag needed.
- **StatTrak and Souvenir are mutually exclusive on a single item** — no
  "StatTrak Souvenir" variant exists.

## Trade Up Contract

Sources: [Counter-Strike Wiki](https://counterstrike.fandom.com/wiki/Trade_Up_Contract)
(fetched via the Fandom API, since direct HTML fetch is blocked by
Cloudflare), and official Valve patch notes pulled via Steam's
`ISteamNews/GetNewsForApp` API (appid 730) — not third-party recaps.

- 10 skins of identical rarity, all-normal **or** all-StatTrak (never mixed)
  → 1 skin of the next-highest rarity, from a collection represented by one
  of the 10 inputs. Inputs may span multiple collections (since the
  2014-05-14 patch).
- Output collection is picked randomly from a pool weighted, approximately,
  by how many inputs came from each collection. **The exact weighting
  algorithm is not published by Valve** — treat any implementation as an
  approximation of community-observed behavior, not a documented spec.
- Rarity ladder: Consumer Grade → Industrial Grade → Mil-Spec → Restricted →
  Classified → Covert. Consumer Grade and Contraband are never valid
  trade-up **outputs**.
- A skin cannot be used as input if its own collection has no skin at the
  next rarity tier up (wiki's example: `Tec-9 | Ossified` in the Aztec
  Collection — Mil-Spec, but Aztec has no Restricted skin). This is derivable
  directly from `Skin.collection_id` + `Skin.rarity_name` — no extra data
  needed.
- Output float: `f = x·(y−z) + z`, where `x` = average float of the 10
  inputs, `y`/`z` = the output skin's `max_float`/`min_float`.
- **2025-10-22 update**: 5 Covert skins → 1 knife or gloves from a collection
  represented by the inputs; 5 StatTrak Covert → 1 StatTrak knife. Knives and
  gloves themselves still cannot be used as trade-up *inputs* — only produced
  as this special-cased output.
- **2026-05-22 update**: Souvenir items can now be selected as trade-up
  inputs, mixable with normal-quality items (not with StatTrak — moot anyway,
  since no StatTrak Souvenir exists). All Souvenir attributes are stripped;
  the output is always a normal-quality item, never Souvenir.

## Sufficiency for a future trade-up simulator

Current schema (`collection_id`, `rarity_name`/`rarity_color`, `stattrak`,
`souvenir`, `min_float`/`max_float`) covers everything above except one
thing: **rarity has no explicit numeric rank**. Weapon skins, knives, and
gloves use different name strings for the same conceptual tier (e.g. gloves'
top tier is "Extraordinary," not "Covert"), so sorting must go by
`rarity_color`, which is consistent across categories — a color→rank ladder
needs to be hardcoded (grey → light blue → blue → purple → pink → red →
terminal gold "Contraband," which is excluded from normal progression). No
new import fields required for this.

## Market items & pricing

`skins_not_grouped.json` has one row per actual tradeable variant — wear ×
Normal/StatTrak/Souvenir — each carrying its own Steam-canonical
`market_hash_name`. `Skin` doesn't store wear or `market_hash_name` directly
(see docs/signals.md): a variant's market_hash_name is reconstructed on
demand from `Skin.name` + StatTrak/Souvenir prefix + a wear bucket name
(`braindamage/cs2cap_api.py::_market_hash_name`), and wear-specific price
data lives in that skin's JSON signal files, tagged by `wear_name`.

- **Doppler/Gamma Doppler collision**: 122 of the ~20.9k distinct
  `market_hash_name` values are shared by multiple `skin_id` patterns — one
  per Doppler phase (Ruby, Sapphire, Black Pearl, Phase 1-4) or Gamma Doppler
  phase (Emerald, Phase 1-4), since Steam's listing name doesn't encode
  phase. `pattern.id` does (e.g. `am_doppler_phase2`, `am_ruby_marbleized`),
  so `Skin.phase` is derived from it via substring match
  (`scripts/import_catalog.py::_derive_phase`). The real tradeable identity
  is `(market_hash_name, phase)`, not `market_hash_name` alone — CS2Cap's
  `GET /prices` takes `phase` as a separate parameter for exactly this.
- CS2Cap's price adapter (`braindamage/cs2cap_api.py`) uses `GET /prices`,
  passing `market_hash_name` and, when set, `phase` — one request per wear
  bucket per skin. `POST /prices/batch` would let one request cover 100
  items instead, but it requires a Starter+ CS2Cap plan (confirmed via a
  live 403 on Free) and has no `phase` parameter at all, so it can't
  disambiguate Doppler phases even for paid tiers where it's available. Not
  used for that reason plus the tier gate.
- CS2Cap's `lowest_ask` is a sell-side/ask price only (cheapest current
  listing) — there's no buy-order data on this endpoint. All price signals
  in this app are ask-side for that reason; a `source` field on each signal
  exists generically for future sources that report bids differently.
