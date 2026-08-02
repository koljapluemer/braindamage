import pandas as pd
import streamlit as st

from braindamage import mono_trade
from braindamage.db import SessionLocal

st.set_page_config(page_title="Simple Mono Trades - braindamage", layout="wide")

st.title("Simple Mono Trades")
st.caption(
    "For every collection x tier x StatTrak/not that can be traded up, this takes the "
    "single cheapest eligible input skin and prices out a contract of 10x just that one "
    "item — the laziest possible trade-up per tier. Simulated the same way as the "
    "Trade-Up simulator (same EV math, same price sources), just batched across every "
    "tier instead of one contract at a time. Top 25 by net expected value."
)

TOP_N = 25


@st.cache_data(show_spinner=False, ttl=300)
def _all_mono_trades() -> list[mono_trade.MonoTradeCandidate]:
    # Progress bar lives in a placeholder, cleared before return: on a cache miss
    # this renders and updates it, then removes it; on a cache hit Streamlit
    # replays the function's final element state, which is the emptied
    # placeholder — i.e. nothing. This is the documented pattern for showing
    # transient progress inside a st.cache_data function.
    progress = st.empty()

    def _on_progress(done: int, total: int) -> None:
        progress.progress(
            done / total,
            text=f"Surveying every collection/tier/StatTrak combo... ({done}/{total})",
        )

    with SessionLocal() as session:
        # Unfiltered and uncapped: the cache holds every combo so the max-cost
        # filter below (which varies per rerun) can be applied without a cache
        # miss, then top-N is taken from whatever survives the filter.
        result = mono_trade.find_mono_trades(session, top_n=None, on_progress=_on_progress)

    progress.empty()
    return result


all_candidates = _all_mono_trades()

max_cost = st.number_input(
    "Max contract cost (10x input, $)",
    min_value=0.0,
    value=0.0,
    step=10.0,
    help="Filters out contracts whose 10x input cost exceeds this. 0 = no limit.",
)

candidates = [c for c in all_candidates if max_cost <= 0 or c.result.input_cost <= max_cost]
candidates.sort(key=lambda c: c.result.expected_value, reverse=True)
candidates = candidates[:TOP_N]

if not candidates:
    st.info(
        "No priced mono trades found for this filter — check that both skin and price data have been "
        "imported, or raise the max cost."
    )
else:
    with SessionLocal() as session:
        favorites = mono_trade.favorite_keys(session)

    rows = []
    for c in candidates:
        result = c.result
        rows.append(
            {
                "★": "★" if c.favorite_key in favorites else "",
                "Collection": c.collection_name,
                "Tier": c.rarity_name,
                "StatTrak™": c.stattrak,
                "Cheapest input": c.skin_name,
                "Wear": c.wear_name or "—",
                "Unit price": c.unit_price,
                "Input cost (10x)": result.input_cost,
                "Expected output value (gross)": result.expected_output_value,
                "Expected value (net of 15% fee)": result.expected_value,
                "ROI": result.roi,
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format(
            {
                "Unit price": "${:,.2f}",
                "Input cost (10x)": "${:,.2f}",
                "Expected output value (gross)": "${:,.2f}",
                "Expected value (net of 15% fee)": "${:,.2f}",
                "ROI": "{:.1%}",
            },
            na_rep="—",
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Favorite / unfavorite")
    st.caption("Favorited contracts show up with live stats (CVaR, outcome distribution) on the Favorites page.")
    for c in candidates:
        is_favorite = c.favorite_key in favorites
        icon_col, label_col = st.columns([1, 11])
        clicked = icon_col.button(
            "★" if is_favorite else "☆",
            key=f"mono_fav_toggle_{c.collection_id}_{c.rarity_name}_{c.stattrak}",
            help="Unfavorite" if is_favorite else "Favorite",
        )
        label_col.write(
            f"{c.collection_name} — {c.rarity_name}{' (StatTrak™)' if c.stattrak else ''} — "
            f"{c.skin_name} ({c.wear_name or '—'}) — "
            f"input ${c.result.input_cost:,.2f} · EV net ${c.result.expected_value:,.2f}"
        )
        if clicked:
            with SessionLocal() as session:
                if is_favorite:
                    mono_trade.remove_favorite(session, c.collection_id, c.rarity_name, c.stattrak)
                else:
                    mono_trade.add_favorite(session, c.collection_id, c.rarity_name, c.stattrak)
            st.rerun()

    with st.expander("Missing price data by row"):
        any_missing = False
        for c in candidates:
            result = c.result
            missing = result.missing_input_price_names + result.missing_output_price_names
            if missing:
                any_missing = True
                st.caption(
                    f"{c.collection_name} — {c.skin_name} ({c.rarity_name}"
                    f"{', StatTrak™' if c.stattrak else ''}): missing prices for "
                    + ", ".join(missing)
                )
        if not any_missing:
            st.caption("None — every row above has full price coverage.")
