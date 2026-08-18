from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import offer_combos, signals
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


@dataclass
class FakeOffer:
    """Minimal PricedFloatOffer -- offer_combos only ever touches .price and
    .float_value, regardless of what real signal type (CSFloat's
    MarketOfferSignal, Steam's SteamOfferSignal) carries them."""

    price: float
    float_value: float


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


class TestBestCombosForSkin:
    def test_needs_at_least_ten_offers(self, session):
        _setup_mono_pair(session)
        offers = [FakeOffer(price=float(i), float_value=0.02) for i in range(9)]
        skin = session.get(Skin, "in-a")

        assert offer_combos.best_combos_for_skin(session, skin, offers) == []

    def test_picks_cheapest_ten_and_prices_correctly(self, session):
        _setup_mono_pair(session)
        offers = [FakeOffer(price=float(i), float_value=0.02 + i * 0.001) for i in range(12)]
        skin = session.get(Skin, "in-a")

        results = offer_combos.best_combos_for_skin(session, skin, offers, top_n=1)

        assert len(results) == 1
        best = results[0]
        assert best.real_cost == pytest.approx(sum(range(10)))
        assert best.outcomes[0].predicted_wear == "Factory New"
        assert best.expected_value == pytest.approx(100.0 * 0.85 - best.real_cost)

    def test_invalid_input_skin_returns_nothing(self, session):
        _make_skin(session, id="in-covert", name="Input Covert", rarity_name="Covert")
        offers = [FakeOffer(price=float(i), float_value=0.02) for i in range(10)]
        skin = session.get(Skin, "in-covert")

        assert offer_combos.best_combos_for_skin(session, skin, offers) == []
