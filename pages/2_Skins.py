import pandas as pd
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from braindamage.db import SessionLocal
from braindamage.models import (
    Collection,
    HourlyListingPrice,
    HourlyMarketAggregate,
    MarketItem,
    PriceObservation,
)

st.set_page_config(page_title="Skins - braindamage", layout="wide")

st.title("Skins by collection & tier")

COLUMNS = [
    "name",
    "weapon_name",
    "category_name",
    "stattrak",
    "souvenir",
    "has_normal_variant",
    "min_float",
    "max_float",
]
COLUMN_LABELS = {
    "name": "Name",
    "weapon_name": "Weapon",
    "category_name": "Category",
    "stattrak": "Has StatTrak",
    "souvenir": "Has Souvenir",
    "has_normal_variant": "Has normal version",
    "min_float": "Min float",
    "max_float": "Max float",
    "last_price": "Last known price",
}

# A bare st.text_input reruns (and re-renders every expander/dataframe on this page)
# on every keystroke, which is what was crashing the browser. A form defers that
# rerun until the filter is submitted, so typing itself does no work.
with st.form("skin_filter"):
    filter_input = st.text_input("Filter", placeholder="Filter by skin or collection name")
    st.form_submit_button("Apply filter")
filter_text = filter_input.strip().lower()

with SessionLocal() as session:
    collections = session.scalars(
        select(Collection).options(selectinload(Collection.skins)).order_by(Collection.name)
    ).all()

    # Most recent price per skin, across all of that skin's market items (any
    # wear/StatTrak/Souvenir variant) and any price source — "last known price" is
    # intentionally coarse; per-variant pricing belongs on a future skin detail view.
    #
    # Three price sources feed this, each shaped differently, so each is reduced to
    # "latest price per skin" separately and merged in Python rather than one big
    # UNION + window-by-skin query: hourly_listing_prices/hourly_market_aggregates
    # can hold tens of millions of rows (see docs/hourly-price-schema.md), and
    # windowing that directly by skin_id would force SQLite to sort the whole table
    # (skin_id isn't a column on those tables, so no index covers it). Reducing to
    # "latest per market_item_id" first keeps each sort scoped to one item's rows —
    # small partitions — and only ~21k market items get joined up to skin_id after.
    latest_po_ts = func.coalesce(PriceObservation.observed_at, PriceObservation.fetched_at)
    po_ranked = (
        select(
            MarketItem.skin_id.label("skin_id"),
            PriceObservation.price.label("price"),
            PriceObservation.currency.label("currency"),
            latest_po_ts.label("ts"),
            func.row_number()
            .over(partition_by=MarketItem.skin_id, order_by=latest_po_ts.desc())
            .label("rn"),
        )
        .join(PriceObservation, PriceObservation.market_item_id == MarketItem.id)
        .subquery()
    )
    po_rows = session.execute(
        select(po_ranked.c.skin_id, po_ranked.c.price, po_ranked.c.currency, po_ranked.c.ts)
        .where(po_ranked.c.rn == 1)
    ).all()

    def _latest_per_skin_from_hourly(value_col, ts_col, table):
        per_item = (
            select(
                table.market_item_id.label("market_item_id"),
                value_col.label("price"),
                ts_col.label("ts"),
                func.row_number()
                .over(partition_by=table.market_item_id, order_by=ts_col.desc())
                .label("rn"),
            )
            .where(value_col.isnot(None))
            .subquery()
        )
        per_skin = (
            select(
                MarketItem.skin_id.label("skin_id"),
                per_item.c.price.label("price"),
                per_item.c.ts.label("ts"),
                func.row_number()
                .over(partition_by=MarketItem.skin_id, order_by=per_item.c.ts.desc())
                .label("rn"),
            )
            .select_from(per_item)
            .join(MarketItem, MarketItem.id == per_item.c.market_item_id)
            .where(per_item.c.rn == 1)
            .subquery()
        )
        return session.execute(
            select(per_skin.c.skin_id, per_skin.c.price, per_skin.c.ts).where(per_skin.c.rn == 1)
        ).all()

    # Both cs2.sh files document monetary values as USD.
    listing_rows = _latest_per_skin_from_hourly(
        HourlyListingPrice.close_ask, HourlyListingPrice.bucket, HourlyListingPrice
    )
    aggregate_rows = _latest_per_skin_from_hourly(
        HourlyMarketAggregate.ask, HourlyMarketAggregate.bucket, HourlyMarketAggregate
    )

    latest_by_skin: dict[str, tuple[float, str, object]] = {}
    for row in po_rows:
        latest_by_skin[row.skin_id] = (row.price, row.currency, row.ts)
    for rows in (listing_rows, aggregate_rows):
        for row in rows:
            existing = latest_by_skin.get(row.skin_id)
            if existing is None or row.ts > existing[2]:
                latest_by_skin[row.skin_id] = (row.price, "USD", row.ts)

    last_price_by_skin = {
        skin_id: f"{price:.2f} {currency}" for skin_id, (price, currency, _ts) in latest_by_skin.items()
    }

if not collections:
    st.info("No data yet. Run an import first.")
else:
    any_shown = False
    for collection in collections:
        collection_matches = bool(filter_text) and filter_text in collection.name.lower()
        if not filter_text or collection_matches:
            skins = collection.skins
        else:
            skins = [s for s in collection.skins if filter_text in s.name.lower()]

        if not skins:
            continue

        groups: dict[str, list] = {}
        for skin in skins:
            groups.setdefault(skin.rarity_name or "Unknown", []).append(skin)

        for rarity_name in sorted(groups):
            any_shown = True
            group_skins = groups[rarity_name]
            with st.expander(f"{collection.name} — {rarity_name} ({len(group_skins)})"):
                rows = []
                for skin in group_skins:
                    row = {col: getattr(skin, col) for col in COLUMNS}
                    row["last_price"] = last_price_by_skin.get(skin.id, "—")
                    rows.append(row)
                df = pd.DataFrame(rows)
                df = df.sort_values("name").rename(columns=COLUMN_LABELS)
                st.dataframe(df, hide_index=True, use_container_width=True)

    if not any_shown:
        st.info("No skins match this filter.")
