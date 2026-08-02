import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import select

from braindamage import mono_trade, tradeup
from braindamage.db import SessionLocal
from braindamage.models import Collection

st.set_page_config(page_title="Favorite Mono Trades - braindamage", layout="wide")

st.title("Favorite Mono Trades")
st.caption(
    "Contracts starred on the Simple Mono Trades page, re-simulated against current prices — the "
    "cheapest input for a combo can shift over time, so this always reflects the current one, not a "
    "frozen snapshot. Includes risk stats a top-25 ranking can't show: CVaR(5%), the average profit "
    "in the worst 5% of the outcome distribution, and the full outcome distribution itself."
)

ALPHA = 0.05


@st.cache_data(show_spinner=False, ttl=300)
def _collection_names() -> dict[str, str]:
    with SessionLocal() as session:
        rows = session.execute(select(Collection.id, Collection.name)).all()
    return {row.id: row.name for row in rows}


with SessionLocal() as session:
    keys = mono_trade.favorite_keys(session)

if not keys:
    st.info("No favorites yet — star a contract on the Simple Mono Trades page to see it here.")
else:
    collection_names = _collection_names()
    ordered_keys = sorted(keys, key=lambda k: (collection_names.get(k[0], k[0]), k[1], k[2]))

    for collection_id, rarity_name, stattrak in ordered_keys:
        collection_name = collection_names.get(collection_id, collection_id)
        header = f"{collection_name} — {rarity_name}{' (StatTrak™)' if stattrak else ''}"

        st.divider()
        title_col, unfav_col = st.columns([10, 1])
        title_col.subheader(header)
        if unfav_col.button(
            "★", key=f"mono_unfav_{collection_id}_{rarity_name}_{stattrak}", help="Unfavorite"
        ):
            with SessionLocal() as session:
                mono_trade.remove_favorite(session, collection_id, rarity_name, stattrak)
            st.rerun()

        with SessionLocal() as session:
            candidate = mono_trade.mono_trade_candidate_for(session, collection_id, rarity_name, stattrak)

        if candidate is None:
            st.warning("No priced, eligible cheapest input for this combo right now.")
            continue

        result = candidate.result
        st.caption(
            f"Cheapest input: {candidate.skin_name} ({candidate.wear_name or '—'}) — "
            f"${candidate.unit_price:,.2f} each"
        )

        profits = tradeup.outcome_profits(result)
        cvar_5 = tradeup.cvar(profits, alpha=ALPHA)

        metric_cols = st.columns(5)
        metric_cols[0].metric("Input cost", f"${result.input_cost:,.2f}")
        metric_cols[1].metric("EV (gross)", f"${result.expected_output_value:,.2f}")
        metric_cols[2].metric("EV (net of 15% fee)", f"${result.expected_value:,.2f}")
        metric_cols[3].metric("ROI", f"{result.roi:.1%}" if result.roi is not None else "—")
        metric_cols[4].metric(
            f"CVaR ({ALPHA:.0%})",
            f"${cvar_5:,.2f}" if cvar_5 is not None else "—",
            help="Probability-weighted average profit within the worst 5% of the outcome distribution.",
        )

        if result.missing_input_price_names:
            st.warning("No price data for input(s): " + ", ".join(result.missing_input_price_names))
        if result.missing_output_price_names:
            st.warning(
                "No price data for possible output(s): " + ", ".join(result.missing_output_price_names)
            )

        chart_df = pd.DataFrame(
            {
                "Outcome": [o.skin_name for o in result.outcomes],
                "Profit": [p for p, _ in profits],
                "Probability": [prob for _, prob in profits],
            }
        ).sort_values("Profit")
        chart_df["Sign"] = chart_df["Profit"].apply(lambda v: "Win" if v >= 0 else "Loss")

        st.caption(
            "One bar per possible output skin. Bar direction/color is the outcome — up (blue) means "
            "that skin sells for more than the 10 inputs cost, down (red) means it sells for less. "
            "Bar width is that outcome's probability — wider means more likely."
        )
        zero_rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#898781").encode(y="y:Q")
        bars = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X("Outcome:N", sort=list(chart_df["Outcome"]), title=None, axis=alt.Axis(labelAngle=-40)),
                y=alt.Y("Profit:Q", title="Profit if this outcome hits", axis=alt.Axis(format="$,.0f")),
                size=alt.Size(
                    "Probability:Q", scale=alt.Scale(range=[8, 45]), legend=alt.Legend(title="Probability", format="%")
                ),
                color=alt.Color(
                    "Sign:N",
                    scale=alt.Scale(domain=["Win", "Loss"], range=["#2a78d6", "#e34948"]),
                    legend=alt.Legend(title=None),
                ),
                tooltip=[
                    alt.Tooltip("Outcome:N", title="Skin"),
                    alt.Tooltip("Probability:Q", format=".2%"),
                    alt.Tooltip("Profit:Q", format="$,.2f"),
                ],
            )
        )
        st.altair_chart((zero_rule + bars).properties(height=340), use_container_width=True)
