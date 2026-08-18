from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import signals, steam_offer_combos
from braindamage.models import Base, Skin
from braindamage.signals import SteamOfferSignal, now_utc


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
) -> Skin:
    skin = Skin(
        id=id,
        name=name,
        weapon_name="Weapon",
        pattern_name="Pattern",
        category_name="Rifle",
        rarity_name=rarity_name,
        rarity_color=None,
        min_float=0.0,
        max_float=1.0,
        stattrak=False,
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


def _offer(price: float, float_value: float, pattern_seed: int = 1, *, age: timedelta = timedelta(hours=1)) -> SteamOfferSignal:
    return SteamOfferSignal(
        market_hash_name="x",
        wear_name="Factory New",
        float_value=float_value,
        pattern_seed=pattern_seed,
        price=price,
        currency="USD",
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
    def test_dedups_by_float_pattern_price_keeping_freshest(self, session):
        _setup_mono_pair(session)
        signals.append_steam_offers(
            "in-a",
            [
                _offer(price=5.0, float_value=0.03, pattern_seed=7, age=timedelta(hours=2)),
                _offer(price=5.0, float_value=0.03, pattern_seed=7, age=timedelta(minutes=1)),  # same triple, fresher
                _offer(price=6.0, float_value=0.03, pattern_seed=7, age=timedelta(hours=1)),  # different price -- kept separately
                _offer(price=1.0, float_value=0.05, pattern_seed=9, age=timedelta(hours=25)),  # stale -- dropped
            ],
        )

        fresh = steam_offer_combos._fresh_offers_by_skin(session)

        assert set(fresh.keys()) == {"in-a"}
        prices = sorted(o.price for o in fresh["in-a"])
        assert prices == [5.0, 6.0]

    def test_offers_without_float_are_dropped(self, session):
        _setup_mono_pair(session)
        signals.append_steam_offers(
            "in-a",
            [
                SteamOfferSignal(
                    market_hash_name="x", wear_name="Factory New", float_value=None,
                    pattern_seed=1, price=5.0, currency="USD", fetched_at=now_utc(),
                )
            ],
        )

        fresh = steam_offer_combos._fresh_offers_by_skin(session)

        assert fresh == {}


class TestFindBestCombos:
    def test_ranks_globally_across_skins(self, session):
        _setup_mono_pair(session, input_id="in-a", output_id="out-a")
        _setup_mono_pair(session, input_id="in-b", output_id="out-b")
        signals.append_steam_offers(
            "in-a", [_offer(price=1.0, float_value=0.02 + i * 0.001, pattern_seed=i) for i in range(10)]
        )
        signals.append_steam_offers(
            "in-b", [_offer(price=50.0, float_value=0.02 + i * 0.001, pattern_seed=i) for i in range(10)]
        )

        combos = steam_offer_combos.find_best_combos(session, top_n=3)

        assert len(combos) == 2
        assert combos[0].input_skin.id == "in-a"
        assert combos[1].input_skin.id == "in-b"
        assert combos[0].expected_value > combos[1].expected_value
