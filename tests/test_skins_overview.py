from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import signals, skins_overview
from braindamage.models import Base, Skin
from braindamage.signals import now_utc


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(signals, "SKINS_DIR", tmp_path / "skins")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _skin(session, skin_id, name, *, rarity="Mil-Spec Grade", stattrak=False, souvenir=False,
          category="Rifle", collection_id="collection-a", collection_name="Collection A"):
    skin = Skin(
        id=skin_id, name=name, weapon_name="Weapon", pattern_name="Pattern",
        category_name=category, rarity_name=rarity, rarity_color=None,
        min_float=0.0, max_float=1.0, stattrak=stattrak, souvenir=souvenir,
        phase=None, paint_index=None, collection_id=collection_id,
        collection_name=collection_name, image_url=None,
    )
    session.add(skin)
    session.flush()
    return skin


def _buy_order(skin_id, wear, price, fetched_at):
    signals.append_buy_order_summaries(skin_id, [signals.BuyOrderSummarySignal(
        market_hash_name=f"X ({wear})", wear_name=wear, price=price, num_orders=5, fetched_at=fetched_at,
    )])


def _legacy(skin_id, wear, price):
    signals.write_legacy_price_snapshot(skin_id, signals.LegacyPriceSnapshot(
        generated_at=datetime(2026, 1, 3),
        prices_by_wear={wear: signals.LegacyWearPrice(price=price, observed_at=datetime(2026, 1, 1))},
    ))


def test_groups_by_collection_and_rarity(session):
    _skin(session, "a", "A skin", rarity="Consumer Grade", collection_id="c1", collection_name="C1")
    _skin(session, "b", "B skin", rarity="Restricted", collection_id="c1", collection_name="C1")
    _skin(session, "c", "C skin", rarity="Consumer Grade", collection_id="c2", collection_name="C2")

    result = skins_overview.build_skins_overview(session)

    assert [c["collection_name"] for c in result["collections"]] == ["C1", "C2"]
    c1 = result["collections"][0]
    # Ladder order: Consumer Grade before Restricted, regardless of insertion order.
    assert [r["rarity_name"] for r in c1["rarities"]] == ["Consumer Grade", "Restricted"]
    assert [s["skin_name"] for s in c1["rarities"][0]["skins"]] == ["A skin"]


def test_stattrak_and_souvenir_variants_are_excluded(session):
    _skin(session, "a", "Normal")
    _skin(session, "b", "ST", stattrak=True)
    _skin(session, "c", "Souv", souvenir=True)

    result = skins_overview.build_skins_overview(session)

    names = [s["skin_name"] for c in result["collections"] for r in c["rarities"] for s in r["skins"]]
    assert names == ["Normal"]


def test_own_wear_price_is_buy_order_without_fee_and_colored_by_age(session):
    _skin(session, "a", "A skin", rarity="Restricted")
    _buy_order("a", "Factory New", 100.0, now_utc())

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]
    fn_cell = next(w for w in skin_row["wears"] if w["wear_name"] == "Factory New")

    # No Steam sell-fee net applied -- the raw buy-order price, unlike an
    # outcome's net_sell_price_for_wear.
    assert fn_cell["value"] == 100.0
    assert fn_cell["color"] == "purple"


def test_own_wear_price_falls_back_to_grey_legacy_price(session):
    _skin(session, "a", "A skin", rarity="Restricted")
    _legacy("a", "Factory New", 42.0)

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]
    fn_cell = next(w for w in skin_row["wears"] if w["wear_name"] == "Factory New")

    assert fn_cell["value"] == 42.0
    assert fn_cell["color"] == "grey"


def test_own_wear_price_is_none_without_any_signal(session):
    _skin(session, "a", "A skin", rarity="Restricted")

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]
    fn_cell = next(w for w in skin_row["wears"] if w["wear_name"] == "Factory New")

    assert fn_cell["value"] is None
    assert fn_cell["color"] is None


def test_snapshot_letters_grey_without_comprehensive_scrape(session):
    _skin(session, "a", "A skin", rarity="Restricted")

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]

    assert skin_row["steam_snapshot_color"] == "grey"
    assert skin_row["csfloat_snapshot_color"] == "grey"
    assert skin_row["steam_url"].startswith("https://steamcommunity.com/")


def test_snapshot_letters_ignore_non_comprehensive_scrapes(session):
    _skin(session, "a", "A skin", rarity="Restricted")
    signals.append_steam_offers("a", [signals.SteamOfferSignal(
        market_hash_name="A skin (Factory New)", wear_name="Factory New", price=1.0,
        fetched_at=now_utc(), comprehensive=False,
    )])

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]

    assert skin_row["steam_snapshot_color"] == "grey"


def test_snapshot_letters_colored_by_comprehensive_scrape_age(session):
    _skin(session, "a", "A skin", rarity="Restricted")
    signals.append_steam_offers("a", [signals.SteamOfferSignal(
        market_hash_name="A skin (Factory New)", wear_name="Factory New", price=1.0,
        fetched_at=now_utc() - timedelta(days=2), comprehensive=True,
    )])
    signals.append_market_offers("a", [signals.MarketOfferSignal(
        source="csfloat", listing_id="l1", market_hash_name="A skin (Factory New)",
        wear_name="Factory New", price=1.0, listing_type="buy_now",
        fetched_at=now_utc(), comprehensive=True,
    )])

    result = skins_overview.build_skins_overview(session)
    skin_row = result["collections"][0]["rarities"][0]["skins"][0]

    assert skin_row["steam_snapshot_color"] == "orange"
    assert skin_row["csfloat_snapshot_color"] == "purple"


def test_group_avg_prices_average_outcome_group_with_fee_included(session):
    _skin(session, "in", "Input", rarity="Mil-Spec Grade")
    _skin(session, "out1", "Out1", rarity="Restricted")
    _skin(session, "out2", "Out2", rarity="Restricted")
    _legacy("out1", "Battle-Scarred", 10.0)
    _legacy("out2", "Battle-Scarred", 20.0)

    result = skins_overview.build_skins_overview(session)
    rarity = result["collections"][0]["rarities"][0]
    assert rarity["rarity_name"] == "Mil-Spec Grade"

    # net_sell_price_for_wear nets Steam's fee, so the average must be below
    # the raw (10+20)/2 == 15.0 midpoint.
    assert rarity["avg_bs"] is not None
    assert rarity["avg_bs"] < 15.0
    assert rarity["avg_fn"] is None  # no Factory New price for either output


def test_group_avg_prices_none_for_ineligible_input(session):
    _skin(session, "a", "Covert skin", rarity="Covert")

    result = skins_overview.build_skins_overview(session)
    rarity = result["collections"][0]["rarities"][0]

    assert rarity["avg_bs"] is None
    assert rarity["avg_fn"] is None


def test_orphaned_collection_is_grouped_separately(session):
    _skin(session, "a", "Orphan", rarity="Contraband", collection_id=None, collection_name=None)

    result = skins_overview.build_skins_overview(session)

    assert result["collections"][0]["collection_name"] == "Unknown collection"
    assert result["collections"][0]["rarities"][0]["avg_bs"] is None
