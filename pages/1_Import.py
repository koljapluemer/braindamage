import streamlit as st
from sqlalchemy import func, select

from braindamage import config, hourly_price_import
from braindamage.csgo_api import run_import
from braindamage.cs2cap_api import run_price_import, select_market_items
from braindamage.csv_price_import import DEFAULT_CSV_PATH, run_csv_price_import
from braindamage.db import SessionLocal
from braindamage.models import Collection, MarketItem, Skin

st.set_page_config(page_title="Import - braindamage", layout="wide")

st.title("Import")
st.write("Source: https://bymykel.com/CSGO-API/#introduction")

with SessionLocal() as session:
    collection_count = session.scalar(select(func.count()).select_from(Collection))
    skin_count = session.scalar(select(func.count()).select_from(Skin))
    market_item_count = session.scalar(select(func.count()).select_from(MarketItem))

col1, col2, col3 = st.columns(3)
col1.metric("Collections in DB", collection_count)
col2.metric("Skins in DB", skin_count)
col3.metric("Market items in DB", market_item_count)

# Feedback from a button click on the *previous* run survives the st.rerun() below via
# session_state — rendering it inline and then calling st.rerun() in the same pass would
# lose it, since the rerun discards whatever the script already drew this pass.
if "catalog_import_feedback" in st.session_state:
    level, message = st.session_state.pop("catalog_import_feedback")
    getattr(st, level)(message)

if st.button("Run import", type="primary"):
    try:
        with st.spinner("Fetching and upserting data..."):
            result = run_import()
    except Exception as exc:
        st.session_state["catalog_import_feedback"] = ("error", f"Import failed: {exc}")
    else:
        st.session_state["catalog_import_feedback"] = (
            "success",
            f"Imported {result.collections} collections, {result.skins} skins, "
            f"and {result.market_items} market items.",
        )
    st.rerun()

st.divider()
st.header("Prices (CS2Cap)")
st.write("Source: https://docs.cs2cap.com/api-reference/prices")

if "price_import_feedback" in st.session_state:
    level, message = st.session_state.pop("price_import_feedback")
    getattr(st, level)(message)

with SessionLocal() as session:
    collections = session.scalars(select(Collection).order_by(Collection.name)).all()

collection_labels = {"All collections": None} | {c.name: c.id for c in collections}
variant_labels = {
    "All variants": None,
    "Normal": "normal",
    "StatTrak": "stattrak",
    "Souvenir": "souvenir",
}

price_col1, price_col2 = st.columns(2)
collection_choice = price_col1.selectbox("Collection", options=list(collection_labels))
variant_choice = price_col2.selectbox("Variant", options=list(variant_labels))

collection_id = collection_labels[collection_choice]
variant = variant_labels[variant_choice]

with SessionLocal() as session:
    matching_count = len(select_market_items(session, collection_id, variant))

# GET /prices is one item per request (POST /prices/batch needs a Starter+ plan), so
# quota cost is 1:1 with the item count — no batching discount.
st.caption(f"{matching_count} market items match — will use {matching_count} API request(s).")
if matching_count > 200:
    st.caption("⚠️ That's a meaningful chunk of a Free-tier monthly quota (1,000 requests).")

disabled_reason = None
if not config.CS2CAP_API_KEY:
    disabled_reason = "Set CS2CAP_API_KEY in your .env file to enable this."
elif matching_count == 0:
    disabled_reason = "No market items match this filter."
if disabled_reason:
    st.caption(f"⚠️ {disabled_reason}")

if st.button("Fetch prices from CS2Cap", type="primary", disabled=bool(disabled_reason)):
    try:
        with st.spinner(f"Fetching prices ({matching_count} request(s))..."):
            result = run_price_import(collection_id=collection_id, variant=variant)
    except Exception as exc:
        st.session_state["price_import_feedback"] = ("error", f"Price fetch failed: {exc}")
    else:
        summary = (
            f"Fetched {result.observations} price observations via {result.requests_made} request(s). "
            f"{result.items_not_found} item(s) had no price data."
        )
        if result.error:
            st.session_state["price_import_feedback"] = (
                "warning",
                f"Stopped early: {result.error}\n\n{summary}",
            )
        else:
            st.session_state["price_import_feedback"] = ("success", summary)
    st.rerun()

st.divider()
st.header("Prices (CSV snapshot)")
st.write(f"Source: `{DEFAULT_CSV_PATH.relative_to(DEFAULT_CSV_PATH.parents[1])}` — a historic, one-off snapshot with no rate limit.")

if "csv_price_import_feedback" in st.session_state:
    level, message = st.session_state.pop("csv_price_import_feedback")
    getattr(st, level)(message)

if st.button("Import prices from CSV", type="primary", disabled=not DEFAULT_CSV_PATH.exists()):
    try:
        with st.spinner("Reading and importing CSV..."):
            csv_result = run_csv_price_import()
    except Exception as exc:
        st.session_state["csv_price_import_feedback"] = ("error", f"CSV import failed: {exc}")
    else:
        st.session_state["csv_price_import_feedback"] = (
            "success",
            f"Read {csv_result.rows_read} row(s), imported {csv_result.observations} price "
            f"observations. {csv_result.items_not_found} cell(s) had no matching market item.",
        )
    st.rerun()

st.divider()
st.header("Prices (cs2.sh hourly historical)")
st.write(
    "Source: `data/32052876/` — CS2 Historical Item Price Dataset (cs2.sh), hourly "
    "OHLC prices from BUFF/CSFloat/Youpin plus a cross-marketplace aggregate, "
    "bundled locally as a one-off historic export. This is tens of millions of rows "
    "— scope the filters below before importing; even a filtered run can take a while."
)

if "hourly_price_import_feedback" in st.session_state:
    level, message = st.session_state.pop("hourly_price_import_feedback")
    getattr(st, level)(message)

dataset_files_exist = hourly_price_import.LISTING_PARQUET.exists() and hourly_price_import.AGGREGATE_PARQUET.exists()

if not dataset_files_exist:
    st.caption("⚠️ Dataset files are missing from `data/32052876/`.")
else:
    dataset_choice = st.radio(
        "Dataset",
        options=["Cross-marketplace aggregate", "Per-source listings"],
        horizontal=True,
        help=(
            "Aggregate: one row per item per hour, ~17M rows total. "
            "Listings: one row per item x source x hour with per-marketplace OHLC ask/bid, ~52M rows total."
        ),
    )

    @st.cache_data(show_spinner=False)
    def _hourly_dataset_bounds():
        return hourly_price_import.get_dataset_bounds()

    bounds_start, bounds_end = _hourly_dataset_bounds()

    hourly_col1, hourly_col2, hourly_col3 = st.columns([2, 2, 3])
    start_date = hourly_col1.date_input(
        "From", value=bounds_start, min_value=bounds_start, max_value=bounds_end
    )
    end_date = hourly_col2.date_input(
        "To (exclusive)", value=bounds_end, min_value=bounds_start, max_value=bounds_end
    )

    if dataset_choice == "Per-source listings":
        selected_sources = hourly_col3.multiselect(
            "Sources", options=list(hourly_price_import.SOURCES), default=list(hourly_price_import.SOURCES)
        )
    else:
        selected_sources = list(hourly_price_import.SOURCES)

    @st.cache_data(show_spinner=False)
    def _hourly_row_estimate(dataset: str, sources: tuple[str, ...], start, end):
        if dataset == "Per-source listings":
            return hourly_price_import.estimate_listing_rows(list(sources), start, end)
        return hourly_price_import.estimate_aggregate_rows(start, end)

    row_estimate = None
    valid_range = start_date < end_date
    valid_sources = dataset_choice != "Per-source listings" or bool(selected_sources)
    if valid_range and valid_sources:
        row_estimate = _hourly_row_estimate(
            dataset_choice, tuple(sorted(selected_sources)), start_date, end_date
        )
        st.caption(f"~{row_estimate:,} row(s) match this filter.")
        if row_estimate > 5_000_000:
            st.caption(
                "⚠️ That's a lot of rows — importing this may take several minutes "
                "and add a meaningful chunk to the local database file."
            )

    disabled_reason = None
    if not valid_range:
        disabled_reason = "'From' must be before 'To'."
    elif not valid_sources:
        disabled_reason = "Select at least one source."
    elif row_estimate == 0:
        disabled_reason = "No rows match this filter."
    if disabled_reason:
        st.caption(f"⚠️ {disabled_reason}")

    if st.button("Import hourly prices", type="primary", disabled=bool(disabled_reason)):
        progress_bar = st.progress(0.0)

        def on_progress(done: int, total: int) -> None:
            fraction = min(done / total, 1.0) if total else 1.0
            progress_bar.progress(fraction, text=f"{done:,} / {total:,} row(s) processed")

        try:
            if dataset_choice == "Per-source listings":
                hourly_result = hourly_price_import.run_listing_price_import(
                    selected_sources, start_date, end_date, progress_callback=on_progress
                )
            else:
                hourly_result = hourly_price_import.run_aggregate_price_import(
                    start_date, end_date, progress_callback=on_progress
                )
        except Exception as exc:
            st.session_state["hourly_price_import_feedback"] = ("error", f"Import failed: {exc}")
        else:
            st.session_state["hourly_price_import_feedback"] = (
                "success",
                f"Read {hourly_result.rows_read:,} row(s), matched {hourly_result.rows_matched:,} "
                f"to a market item ({hourly_result.rows_unmatched:,} unmatched), inserted "
                f"{hourly_result.rows_inserted:,} new row(s) (duplicates from a re-run are skipped).",
            )
        st.rerun()
