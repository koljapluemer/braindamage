from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import mono_trade_overview, signals
from braindamage.models import Base, Skin


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(signals, "SKINS_DIR", tmp_path / "skins")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _skin(session, skin_id, name, rarity="Mil-Spec Grade"):
    skin = Skin(
        id=skin_id, name=name, weapon_name="Weapon", pattern_name="Pattern",
        category_name="Rifle", rarity_name=rarity, rarity_color=None,
        min_float=0.0, max_float=1.0, stattrak=False, souvenir=False,
        phase=None, paint_index=None, collection_id="collection-a",
        collection_name="Collection A", image_url=None,
    )
    session.add(skin)
    session.flush()
    return skin


def _price(skin_id, price):
    signals.append_price_observations(skin_id, [signals.PriceObservationSignal(
        source="test", wear_name="Field-Tested", price=price,
        fetched_at=datetime(2026, 1, 1),
    )])


def _snapshot(skin_id, price):
    signals.write_legacy_price_snapshot(skin_id, signals.LegacyPriceSnapshot(
        generated_at=datetime(2026, 1, 3),
        prices_by_wear={"Field-Tested": signals.LegacyWearPrice(
            price=price, observed_at=datetime(2026, 1, 1),
        )},
    ))


def test_uses_best_persisted_ev_and_marks_price_ranges(session):
    _skin(session, "a", "A rather long skin")
    _skin(session, "b", "B skin")
    _skin(session, "output", "Output", rarity="Restricted")
    _snapshot("a", 0.30)
    _snapshot("b", 0.16)
    signals.append_contract_history("a", [signals.ContractHistorySignal(
        expected_value=-2.0, raw_avg_float=0.2, generated_at=datetime(2026, 1, 1),
    )])
    signals.append_contract_history("b", [signals.ContractHistorySignal(
        expected_value=1.25, raw_avg_float=0.2, generated_at=datetime(2026, 1, 2),
    )])

    result = mono_trade_overview.build_overview(session)

    trade = result["trades"][0]
    assert trade["expected_value"] == 1.25
    assert trade["ev_source"] == "persisted"
    assert [skin["price_emphasis"] for skin in trade["input_skins"]] == ["same_range", "cheapest"]
    assert trade["input_skins"][0]["steam_url"].startswith("https://steamcommunity.com/")


def test_offer_price_takes_precedence_and_naive_ev_is_best_available(session, monkeypatch):
    _skin(session, "a", "Input A")
    _skin(session, "b", "Input B")
    _skin(session, "output", "Output", rarity="Restricted")
    _snapshot("a", 0.01)  # ignored because this skin has a Steam offer
    _snapshot("b", 0.20)
    signals.append_steam_offers("a", [signals.SteamOfferSignal(
        market_hash_name="Input A (Field-Tested)", wear_name="Field-Tested",
        float_value=0.2, pattern_seed=1, price=2.0, fetched_at=datetime(2026, 1, 2),
    )])

    def fake_table(_session, skin, **_kwargs):
        value = -3.0 if skin.id == "a" else -1.0
        return {"rows": [{"ev_cell": {"value": value}}]}

    monkeypatch.setattr(mono_trade_overview.mono_trade_table, "build_table", fake_table)
    trade = mono_trade_overview.build_overview(session)["trades"][0]

    assert trade["expected_value"] == -1.0
    assert trade["ev_source"] == "naive"
    assert [skin["price_emphasis"] for skin in trade["input_skins"]] == [None, "cheapest"]


def test_csfloat_input_source_prices_from_market_offers_not_steam(session):
    _skin(session, "a", "Input A")
    _skin(session, "output", "Output", rarity="Restricted")
    signals.append_steam_offers("a", [signals.SteamOfferSignal(
        market_hash_name="Input A (Field-Tested)", wear_name="Field-Tested",
        float_value=0.2, pattern_seed=1, price=999.0, fetched_at=datetime(2026, 1, 2),
    )])
    signals.append_market_offers("a", [signals.MarketOfferSignal(
        source="csfloat", listing_id="l1", market_hash_name="Input A (Field-Tested)",
        wear_name="Field-Tested", float_value=0.2, price=2.0, listing_type="buy_now",
        fetched_at=datetime(2026, 1, 2),
    )])

    result = mono_trade_overview.build_overview(session, input_source="csfloat")

    # Only the csfloat-sourced $2.00 offer should be visible -- the $999
    # Steam offer must not leak into a csfloat-scoped overview.
    assert result["trades"][0]["ev_source"] == "naive"
    prices_csfloat = mono_trade_overview._latest_offer_prices_by_wear("a", "csfloat")
    prices_steam = mono_trade_overview._latest_offer_prices_by_wear("a", "steam")
    assert prices_csfloat == {"Field-Tested": 2.0}
    assert prices_steam == {"Field-Tested": 999.0}


def test_csfloat_input_source_excludes_auction_listings(session):
    _skin(session, "a", "Input A")
    _skin(session, "output", "Output", rarity="Restricted")
    signals.append_market_offers("a", [
        signals.MarketOfferSignal(
            source="csfloat", listing_id="buy-1", market_hash_name="Input A (Field-Tested)",
            wear_name="Field-Tested", price=9.0, listing_type="buy_now", fetched_at=datetime(2026, 1, 2),
        ),
        signals.MarketOfferSignal(
            source="csfloat", listing_id="auction-1", market_hash_name="Input A (Field-Tested)",
            wear_name="Field-Tested", price=0.01, listing_type="auction", fetched_at=datetime(2026, 1, 2),
        ),
    ])

    prices = mono_trade_overview._latest_offer_prices_by_wear("a", "csfloat")

    assert prices == {"Field-Tested": 9.0}


def test_overview_never_reads_full_legacy_history(session, monkeypatch):
    _skin(session, "a", "Input A")
    _skin(session, "output", "Output", rarity="Restricted")
    _snapshot("a", 1.0)
    _snapshot("output", 20.0)
    for name in ("read_price_observations", "read_aggregated_prices"):
        monkeypatch.setattr(signals, name, lambda _skin_id: pytest.fail("historical price read"))

    result = mono_trade_overview.build_overview(
        session, rarities=["Mil-Spec Grade"], stattrak_values=[False]
    )

    assert len(result["trades"]) == 1
