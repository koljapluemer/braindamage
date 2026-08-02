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
def _top_mono_trades() -> list[mono_trade.MonoTradeCandidate]:
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
        result = mono_trade.find_mono_trades(session, top_n=TOP_N, on_progress=_on_progress)

    progress.empty()
    return result


candidates = _top_mono_trades()

if not candidates:
    st.info("No priced mono trades found — check that both skin and price data have been imported.")
else:
    rows = []
    for c in candidates:
        result = c.result
        rows.append(
            {
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
