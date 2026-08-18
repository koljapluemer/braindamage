from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import mono_offer_combos, pricing, signals
from braindamage.models import Base, Skin
from braindamage.signals import MarketOfferSignal, now_utc


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
    min_float: float = 0.0,
    max_float: float = 1.0,
    category_name: str = "Rifle",
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
        souvenir=False,
        phase=None,
        paint_index=None,
        collection_id=collection_id,
        collection_name=collection_name,
        image_url=None,
    )
    session.add(skin)
    session.flush()
    return skin


def _offer(listing_id: str, price: float, float_value: float = 0.03, *, age: timedelta = timedelta(hours=1)) -> MarketOfferSignal:
    return MarketOfferSignal(
        source="csfloat",
        listing_id=listing_id,
        market_hash_name="x",
        wear_name="Factory New",
        float_value=float_value,
        price=price,
        listing_type="buy_now",
        fetched_at=now_utc() - age,
    )


def _setup_mono_pair(session: Session, *, input_id="in-a", output_id="out-a"):
    _make_skin(session, id=input_id, name="Input A", rarity_name="Mil-Spec Grade")
    _make_skin(session, id=output_id, name="Output A", rarity_name="Restricted")
    signals.append_price_observations(
        output_id,
        [
            signals.PriceObservationSignal(
                source="test", wear_name="Factory New", price=100.0, fetched_at=now_utc()
            )
        ],
    )


class TestFreshOffersBySkin:
    def test_dedups_stale_and_wrong_type_offers(self, session):
        _setup_mono_pair(session)
        signals.append_market_offers(
            "in-a",
            [
                _offer("l1", price=5.0, age=timedelta(hours=1)),
                _offer("l1", price=4.0, age=timedelta(minutes=1)),  # same listing, fresher -- wins
                _offer("l2", price=1.0, age=timedelta(hours=25)),  # stale -- dropped
                MarketOfferSignal(  # not buy-now -- dropped
                    source="csfloat", listing_id="l3", market_hash_name="x", wear_name="Factory New",
                    float_value=0.03, price=1.0, listing_type="auction", fetched_at=now_utc(),
                ),
                MarketOfferSignal(  # no float -- dropped
                    source="csfloat", listing_id="l4", market_hash_name="x", wear_name="Factory New",
                    float_value=None, price=1.0, listing_type="buy_now", fetched_at=now_utc(),
                ),
            ],
        )

        fresh = mono_offer_combos._fresh_offers_by_skin(session)

        assert set(fresh.keys()) == {"in-a"}
        assert [o.listing_id for o in fresh["in-a"]] == ["l1"]
        assert fresh["in-a"][0].price == 4.0


class TestBestCombosForSkin:
    def test_needs_at_least_ten_fresh_offers(self, session):
        _setup_mono_pair(session)
        offers = [_offer(f"l{i}", price=float(i)) for i in range(9)]
        skin = session.get(Skin, "in-a")

        assert mono_offer_combos.best_combos_for_skin(session, skin, offers) == []

    def test_picks_cheapest_ten_and_prices_correctly(self, session):
        _setup_mono_pair(session)
        # 12 candidates, all Factory New floats -- the algorithm should pick the
        # cheapest 10 ($0..$9), leaving out the $10/$11 pair.
        offers = [_offer(f"l{i}", price=float(i), float_value=0.02 + i * 0.001) for i in range(12)]
        skin = session.get(Skin, "in-a")

        results = mono_offer_combos.best_combos_for_skin(session, skin, offers, top_n=3)

        # Every reachable combo lands in the same Factory New output bucket (all
        # floats are well under the 0.07 cutoff), so ranking is driven purely by
        # cost -- the cheapest combo excludes exactly the two priciest offers.
        assert len(results) == 3
        best = results[0]
        assert sorted(o.listing_id for o in best.offers) == [f"l{i}" for i in range(10)]
        assert best.real_cost == pytest.approx(sum(range(10)))  # 45: excludes l10 ($10), l11 ($11)
        assert best.outcomes[0].skin_name == "Output A"
        assert best.outcomes[0].predicted_wear == "Factory New"
        expected_revenue = 100.0 * (1 - 0.15)  # SELL_FEE_RATE
        assert best.expected_value == pytest.approx(expected_revenue - best.real_cost)
        assert best.expected_value >= results[1].expected_value >= results[2].expected_value

    def test_invalid_input_skin_returns_nothing(self, session):
        # Covert has no next rarity tier -- not a valid trade-up input.
        _make_skin(session, id="in-covert", name="Input Covert", rarity_name="Covert")
        offers = [_offer(f"l{i}", price=float(i)) for i in range(10)]
        skin = session.get(Skin, "in-covert")

        assert mono_offer_combos.best_combos_for_skin(session, skin, offers) == []

    def test_dead_end_collection_returns_nothing(self, session):
        # Collection has an input-tier skin but no Restricted-tier output at all.
        _make_skin(session, id="in-dead", name="Input Dead", rarity_name="Mil-Spec Grade", collection_id="col-dead", collection_name="Dead Collection")
        offers = [_offer(f"l{i}", price=float(i)) for i in range(10)]
        skin = session.get(Skin, "in-dead")

        assert mono_offer_combos.best_combos_for_skin(session, skin, offers) == []


class TestFindBestCombos:
    def test_ranks_globally_across_skins(self, session):
        _setup_mono_pair(session, input_id="in-a", output_id="out-a")
        _setup_mono_pair(session, input_id="in-b", output_id="out-b")
        # in-a: cheap inputs, big payout -- clearly better EV than in-b.
        signals.append_market_offers("in-a", [_offer(f"a{i}", price=1.0) for i in range(10)])
        # in-b: expensive inputs, same $100 payout -- worse EV.
        signals.append_market_offers("in-b", [_offer(f"b{i}", price=50.0) for i in range(10)])

        combos = mono_offer_combos.find_best_combos(session, top_n=3)

        assert len(combos) == 2
        assert combos[0].input_skin.id == "in-a"
        assert combos[1].input_skin.id == "in-b"
        assert combos[0].expected_value > combos[1].expected_value
