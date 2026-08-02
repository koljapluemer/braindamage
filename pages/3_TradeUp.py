import pandas as pd
import streamlit as st

from braindamage import pricing, tradeup
from braindamage.db import SessionLocal

st.set_page_config(page_title="Trade-Up - braindamage", layout="wide")

st.title("Trade-Up Contract Simulator")
st.caption(
    "Classic weapon-skin trade-ups only: 10 same-rarity, same-StatTrak inputs → "
    "1 output at the next rarity tier. Knife/glove crafting (Oct 2025 update) and "
    "Souvenir inputs (May 2026 update) aren't modeled yet."
)

# Single dataclass instance is the one source of truth for this page's state — it's
# mutated only right before an explicit st.rerun(), never read-and-mutated in the
# same pass. Widgets below are never bound to this session_state key directly.
if "tradeup_contract" not in st.session_state:
    st.session_state["tradeup_contract"] = tradeup.ContractState()
contract: tradeup.ContractState = st.session_state["tradeup_contract"]


@st.cache_data(show_spinner="Loading eligible skins...", ttl=300)
def _all_input_options() -> list[tradeup.SkinOption]:
    with SessionLocal() as session:
        return tradeup.eligible_input_options(session)


all_options = _all_input_options()

# There's no rarity/StatTrak picker — the contract locks to whichever tier the
# first added item belongs to, and the search below narrows to just that tier
# from then on.
if contract.lines:
    st.caption(
        f"Contract tier: **{contract.rarity_name}**"
        f"{' (StatTrak™)' if contract.stattrak else ''} — locked from your first pick. "
        "Clear the contract below to start a different tier."
    )
    options = [
        o for o in all_options if o.rarity_name == contract.rarity_name and o.stattrak == contract.stattrak
    ]
else:
    options = all_options

st.divider()
st.subheader(f"Inputs ({contract.total_quantity}/10)")

remaining = 10 - contract.total_quantity
if remaining <= 0:
    st.caption("Contract full — remove a line below to add a different item.")
elif not options:
    st.warning("No eligible skins found for this tier — check that data has been imported.")
else:
    option_labels = {o.label: o for o in options}
    # Native selectbox already filters options as you type — that's the
    # "search with autocomplete" behavior, no extra widget needed. Keying on
    # the current line count resets it to empty after every add, so it's ready
    # for the next search rather than still showing the last pick.
    skin_choice = st.selectbox(
        "Search for a skin",
        options=list(option_labels),
        index=None,
        placeholder='Type to search — e.g. "AK-47 Redline", "StatTrak", a collection name...',
        key=f"tradeup_skin_search_{contract.rarity_name}_{contract.stattrak}_{len(contract.lines)}",
    )

    if skin_choice is not None:
        selected = option_labels[skin_choice]

        float_col, wear_col, qty_col = st.columns([2, 2, 1])
        float_choice = float_col.number_input(
            "Float",
            min_value=selected.min_float,
            max_value=selected.max_float,
            value=round((selected.min_float + selected.max_float) / 2, 4),
            step=0.001,
            format="%.4f",
            help="Wear is derived from this, not chosen separately.",
            key=f"tradeup_float_{selected.skin_id}_{selected.stattrak}",
        )

        # Wear is *always* a function of float, never an independent choice —
        # resolve it (with graceful fallback to the nearest wear this skin
        # actually has listings for) and show it read-only, right next to price.
        with SessionLocal() as session:
            preview_item = tradeup.resolve_market_item_by_float(
                session, selected.skin_id, selected.stattrak, float_choice
            )
            preview_price = (
                pricing.latest_prices(session, [preview_item.id]).get(preview_item.id)
                if preview_item
                else None
            )

        if preview_item is None:
            wear_col.metric("Wear (from float)", "—")
            st.warning(f"No market data for {selected.skin_name} — can't add it yet.")
        else:
            wear_col.metric("Wear (from float)", preview_item.wear_name)
            wear_col.caption(f"≈ ${preview_price:,.2f}" if preview_price is not None else "No price data")

        qty_choice = qty_col.number_input(
            "Qty",
            min_value=1,
            max_value=remaining,
            value=1,
            key=f"tradeup_qty_{selected.skin_id}_{selected.stattrak}_{remaining}",
        )

        if st.button("Add to contract", type="primary", disabled=preview_item is None):
            contract.rarity_name = selected.rarity_name
            contract.stattrak = selected.stattrak
            contract.lines.append(
                tradeup.ContractLine(
                    market_item_id=preview_item.id,
                    skin_id=selected.skin_id,
                    skin_name=selected.skin_name,
                    collection_id=selected.collection_id,
                    collection_name=selected.collection_name,
                    wear_name=preview_item.wear_name,
                    float_value=float_choice,
                    quantity=qty_choice,
                )
            )
            st.rerun()

if contract.lines:
    st.divider()
    st.subheader("Current lines")
    lines_df = pd.DataFrame(
        [
            {
                "Collection": line.collection_name,
                "Skin": line.skin_name,
                "Wear": line.wear_name,
                "Float": line.float_value,
                "Qty": line.quantity,
            }
            for line in contract.lines
        ]
    )
    st.dataframe(lines_df, hide_index=True, use_container_width=True)

    remove_cols = st.columns(min(len(contract.lines), 4) or 1)
    for i, line in enumerate(contract.lines):
        if remove_cols[i % len(remove_cols)].button(
            f"Remove: {line.skin_name} ({line.wear_name})", key=f"tradeup_remove_{i}"
        ):
            contract.lines.pop(i)
            st.rerun()

    if st.button("Clear contract"):
        st.session_state["tradeup_contract"] = tradeup.ContractState()
        st.rerun()

st.divider()
if contract.is_ready:
    with SessionLocal() as session:
        try:
            result = tradeup.simulate_contract(session, contract)
        except ValueError as exc:
            st.error(str(exc))
            result = None

    if result is not None:
        st.subheader("Simulation")
        metric_cols = st.columns(4)
        metric_cols[0].metric("Input cost", f"${result.input_cost:,.2f}")
        metric_cols[1].metric("Expected output value (gross)", f"${result.expected_output_value:,.2f}")
        metric_cols[2].metric("Expected value (net of 15% fee)", f"${result.expected_value:,.2f}")
        metric_cols[3].metric("ROI", f"{result.roi:.1%}" if result.roi is not None else "—")

        if result.missing_input_price_names:
            st.warning(
                "No price data for input(s): "
                + ", ".join(result.missing_input_price_names)
                + " — input cost is understated."
            )
        if result.missing_output_price_names:
            st.warning(
                "No price data for possible output(s): "
                + ", ".join(result.missing_output_price_names)
                + " — expected value is understated."
            )

        outcomes_df = pd.DataFrame(
            [
                {
                    "Collection": o.collection_name,
                    "Skin": o.skin_name,
                    "Probability": o.probability,
                    "Predicted wear": o.predicted_wear,
                    "Predicted float": o.predicted_float,
                    "Price (gross)": o.gross_price,
                    "Contribution (net)": o.contribution,
                }
                for o in result.outcomes
            ]
        )
        st.dataframe(
            outcomes_df.style.format(
                {
                    "Probability": "{:.2%}",
                    "Predicted float": "{:.4f}",
                    "Price (gross)": "${:,.2f}",
                    "Contribution (net)": "${:,.2f}",
                },
                na_rep="—",
            ),
            hide_index=True,
            use_container_width=True,
        )
else:
    st.info(f"Add {10 - contract.total_quantity} more item(s) to run the simulation.")
