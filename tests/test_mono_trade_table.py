from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import mono_trade_table, signals, steam_fees
from braindamage.models import Base, Skin
from braindamage.signals import now_utc


@pytest.fixture
def signals_dir(tmp_path, monkeypatch):
    path = tmp_path / "skins"
    monkeypatch.setattr(signals, "SKINS_DIR", path)
    return path


@pytest.fixture
def session(signals_dir):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _make_skin(
    session: Session,
    *,
    id: str,
    name: str,
    rarity_name: str,
    collection_id: str = "col-a",
    collection_name: str = "Collection A",
    stattrak: bool = False,
    souvenir: bool = False,
    category_name: str = "Rifle",
    min_float: float = 0.0,
    max_float: float = 1.0,
) -> Skin:
    skin = Skin(
        id=id,
        name=name,
        weapon_name="Weapon",
        pattern_name="Pattern",
        category_name=category_name,
        rarity_name=rarity_name,
        rarity_color=None,
        min_float=min_float,
        max_float=max_float,
        stattrak=stattrak,
        souvenir=souvenir,
        phase=None,
        paint_index=None,
        collection_id=collection_id,
        collection_name=collection_name,
        image_url=None,
    )
    session.add(skin)
    session.flush()
    return skin


def _offers(skin_id: str, wear_name: str, prices: list[float], *, fetched_at=None) -> None:
    fetched_at = fetched_at or now_utc()
    signals.append_steam_offers(
        skin_id,
        [
            signals.SteamOfferSignal(
                market_hash_name=f"Skin ({wear_name})",
                wear_name=wear_name,
                float_value=0.02 + i * 0.001,
                pattern_seed=i,
                price=price,
                fetched_at=fetched_at,
            )
            for i, price in enumerate(prices)
        ],
    )


class TestBuildTableErrors:
    def test_knife_is_not_a_usable_input(self, session):
        skin = _make_skin(session, id="k", name="Karambit", rarity_name="Covert", category_name="Knives")
        with pytest.raises(mono_trade_table.MonoTradeTableError):
            mono_trade_table.build_table(session, skin)

    def test_covert_has_no_next_rarity(self, session):
        skin = _make_skin(session, id="c", name="AWP | Dragon Lore", rarity_name="Covert")
        with pytest.raises(mono_trade_table.MonoTradeTableError, match="no next rarity"):
            mono_trade_table.build_table(session, skin)

    def test_collection_with_no_eligible_output_raises(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        with pytest.raises(mono_trade_table.MonoTradeTableError, match="no eligible output"):
            mono_trade_table.build_table(session, skin)


class TestBuildTableRows:
    def test_input_cell_is_none_with_fewer_than_ten_offers(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        _offers("in-a", "Field-Tested", [1.0] * 9)

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        assert row["input_cell"] == {"value": None, "color": None}
        assert row["ev_cell"] == {"value": None}

    def test_input_cell_uses_latest_batch_only_when_it_has_enough_offers(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        old = now_utc() - timedelta(days=2)
        # 10 cheap+old offers, then a fresh full batch of 10 pricier ones for
        # the same 10 listings -- the fresh batch alone has enough inputs, so
        # the stale cheap prices must be ignored entirely, value and color
        # both reflecting only the latest scrape.
        _offers("in-a", "Field-Tested", [1.0] * 10, fetched_at=old)
        _offers("in-a", "Field-Tested", [50.0] * 10, fetched_at=now_utc())

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        assert row["input_cell"]["value"] == pytest.approx(500.0)
        assert row["input_cell"]["color"] == "purple"

    def test_input_cell_tops_up_shortfall_from_older_offers_when_batch_is_short(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        old = now_utc() - timedelta(days=2)
        # 10 cheap+old offers, plus a small fresh batch of only 2 -- not
        # enough on its own, so the shortfall (8) is topped up from the
        # cheapest remaining older offers, and the color reflects that oldest
        # data actually used, not the fresh batch's own age.
        _offers("in-a", "Field-Tested", [1.0] * 10, fetched_at=old)
        _offers("in-a", "Field-Tested", [99.0, 99.0], fetched_at=now_utc())

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        # The fresh batch's 2 offers collide (same float/pattern-seed
        # identity, see _offers) with 2 of the 10 old ones, so those 2 old
        # observations are excluded from the top-up pool -- 8 old offers
        # remain, exactly filling the shortfall.
        assert row["input_cell"]["value"] == pytest.approx(2 * 99.0 + 8 * 1.0)
        assert row["input_cell"]["color"] == "orange"

    def test_input_cell_is_none_when_batch_plus_topup_still_short(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        old = now_utc() - timedelta(days=2)
        _offers("in-a", "Field-Tested", [1.0] * 3, fetched_at=old)
        _offers("in-a", "Field-Tested", [50.0] * 3, fetched_at=now_utc())

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        assert row["input_cell"] == {"value": None, "color": None}

    def test_dedups_offers_by_float_pattern_price_keeping_latest(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        # Re-scraping the exact same 10 listings twice must still only count
        # as 10 offers, not 20.
        _offers("in-a", "Field-Tested", list(range(1, 11)))
        _offers("in-a", "Field-Tested", list(range(1, 11)))

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        assert row["input_cell"]["value"] == pytest.approx(sum(range(1, 11)))

    def test_outcome_cell_prefers_buy_order_over_fallback_price(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        _offers("in-a", "Field-Tested", [1.0] * 10)
        signals.append_price_observations(
            "out-a",
            [signals.PriceObservationSignal(source="test", wear_name="Field-Tested", price=50.0, fetched_at=now_utc())],
        )
        signals.append_buy_order_summaries(
            "out-a",
            [
                signals.BuyOrderSummarySignal(
                    market_hash_name="Output A (Field-Tested)",
                    wear_name="Field-Tested",
                    price=80.0,
                    num_orders=10,
                    fetched_at=now_utc(),
                )
            ],
        )

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        cell = row["outcome_cells"][0]
        # Cell value is net of Steam's real sell fee, not the raw $80 buy-order
        # ask -- this table shows what you'd actually walk away with.
        assert cell == {"value": steam_fees.net_proceeds(80.0), "color": "purple", "source": "buy_order"}

    def test_outcome_cell_falls_back_to_grey_when_no_buy_order(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        _offers("in-a", "Field-Tested", [1.0] * 10)
        old = now_utc() - timedelta(days=30)
        signals.append_price_observations(
            "out-a",
            [signals.PriceObservationSignal(source="test", wear_name="Field-Tested", price=50.0, fetched_at=old)],
        )

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        cell = row["outcome_cells"][0]
        assert cell == {"value": steam_fees.net_proceeds(50.0), "color": "grey", "source": "fallback"}

    def test_outcome_cell_is_empty_when_no_price_at_all(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        _offers("in-a", "Field-Tested", [1.0] * 10)

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        assert row["outcome_cells"][0] == {"value": None, "color": None, "source": None}

    def test_ev_weights_by_probability_nets_steam_tax_and_subtracts_input_cost(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        _make_skin(session, id="out-b", name="Output B", rarity_name="Restricted")
        _offers("in-a", "Field-Tested", [1.0] * 10)  # input cost = 10.0
        signals.append_buy_order_summaries(
            "out-a",
            [
                signals.BuyOrderSummarySignal(
                    market_hash_name="Output A (Field-Tested)",
                    wear_name="Field-Tested",
                    price=100.0,
                    num_orders=1,
                    fetched_at=now_utc(),
                )
            ],
        )
        # out-b has no price at all -- contributes 0, per the rest of the
        # app's "missing price = $0" convention (see tradeup.py).

        table = mono_trade_table.build_table(session, skin)

        row = next(r for r in table["rows"] if r["wear_name"] == "Field-Tested")
        # probability = 1/2 per output; only out-a is priced.
        expected_ev = 0.5 * steam_fees.net_proceeds(100.0) - 10.0
        assert row["ev_cell"]["value"] == pytest.approx(expected_ev)

    def test_stattrak_input_only_matches_stattrak_outputs(self, session):
        skin = _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade", stattrak=True)
        _make_skin(session, id="out-normal", name="Output Normal", rarity_name="Restricted", stattrak=False)
        _make_skin(session, id="out-st", name="Output ST", rarity_name="Restricted", stattrak=True)

        table = mono_trade_table.build_table(session, skin)

        assert [h["skin_name"] for h in table["outcome_headers"]] == ["Output ST"]


class TestSteamListingUrl:
    def test_urls_are_valid_steam_listing_links(self, session):
        skin = _make_skin(session, id="in-a", name="AK-47 | Redline", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")

        table = mono_trade_table.build_table(session, skin)

        assert table["input_header"]["steam_url"].startswith(
            "https://steamcommunity.com/market/listings/730/"
        )
        assert "%7C" in table["input_header"]["steam_url"]  # the "|" got url-encoded
