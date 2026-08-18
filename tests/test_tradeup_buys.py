import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from braindamage import signals, steamapis_api, tradeup_buys
from braindamage.models import Base, Skin
from braindamage.tradeup import WEAR_BUCKETS


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
        min_float=0.0,
        max_float=1.0,
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


def _quote(price: float) -> steamapis_api.CsfloatQuote:
    return steamapis_api.CsfloatQuote(
        market_hash_name="x", price=price, offer_count=1, updated_at=1700000000, raw={"priceUSD": price}
    )


class TestSurveyCheapestTradeupBuys:
    def _setup_group_of_three(self, session):
        # All three are legal Mil-Spec inputs for Collection A (a Restricted output
        # exists), priced $10/$5/$20 -- cheapest first should be "in-b".
        _make_skin(session, id="in-a", name="Input A", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="in-b", name="Input B", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="in-c", name="Input C", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")

    def test_ranks_cheapest_first_and_caps_at_top_n(self, session, monkeypatch):
        self._setup_group_of_three(session)
        prices = {"Input A": 10.0, "Input B": 5.0, "Input C": 20.0}

        def fake_fetch(market_hash_name):
            for skin_name, price in prices.items():
                if market_hash_name.startswith(skin_name):
                    return _quote(price)
            return None

        monkeypatch.setattr(steamapis_api, "fetch_csfloat_price", fake_fetch)

        result = tradeup_buys.survey_cheapest_tradeup_buys(session, top_n_per_group=2)

        assert result.error is None
        assert len(result.groups) == 1
        group = result.groups[0]
        assert group.collection_id == "col-a"
        assert group.rarity_name == "Mil-Spec Grade"
        assert [c.skin.id for c in group.candidates] == ["in-b", "in-a"]  # cheapest 2 of 3
        assert [c.price for c in group.candidates] == [5.0, 10.0]

    def test_writes_price_signals_and_updates_last_price(self, session, monkeypatch):
        self._setup_group_of_three(session)
        monkeypatch.setattr(steamapis_api, "fetch_csfloat_price", lambda name: _quote(7.5))

        tradeup_buys.survey_cheapest_tradeup_buys(session, top_n_per_group=3)

        obs = signals.read_price_observations("in-a")
        assert len(obs) == len(WEAR_BUCKETS)
        assert all(o.source == "steamapis_csfloat" and o.price == 7.5 for o in obs)

        skin = session.get(Skin, "in-a")
        assert skin.last_price == pytest.approx(7.5)

    def test_excludes_stattrak_skins(self, session, monkeypatch):
        self._setup_group_of_three(session)
        _make_skin(session, id="in-st", name="Input ST", rarity_name="Mil-Spec Grade", stattrak=True)
        _make_skin(session, id="out-st", name="Output ST", rarity_name="Restricted", stattrak=True)
        monkeypatch.setattr(steamapis_api, "fetch_csfloat_price", lambda name: _quote(1.0))

        result = tradeup_buys.survey_cheapest_tradeup_buys(session, top_n_per_group=10)

        priced_ids = {c.skin.id for g in result.groups for c in g.candidates}
        assert "in-st" not in priced_ids

    def test_skin_with_no_price_data_is_excluded_but_others_still_reported(self, session, monkeypatch):
        self._setup_group_of_three(session)
        monkeypatch.setattr(
            steamapis_api,
            "fetch_csfloat_price",
            lambda name: None if name.startswith("Input C") else _quote(3.0),
        )

        result = tradeup_buys.survey_cheapest_tradeup_buys(session, top_n_per_group=10)

        priced_ids = {c.skin.id for g in result.groups for c in g.candidates}
        assert priced_ids == {"in-a", "in-b"}

    def test_stops_gracefully_on_api_error_but_keeps_prior_progress(self, session, monkeypatch):
        # Two independent groups -- Collection A (Mil-Spec) and Collection B (also
        # Mil-Spec) -- so an error partway through leaves one group's data intact.
        self._setup_group_of_three(session)
        _make_skin(session, id="in-d", name="Input D", rarity_name="Mil-Spec Grade", collection_id="col-b", collection_name="Collection B")
        _make_skin(session, id="out-d", name="Output D", rarity_name="Restricted", collection_id="col-b", collection_name="Collection B")

        def fake_fetch(market_hash_name):
            if market_hash_name.startswith("Input D"):
                raise steamapis_api.SteamApisRateLimitError(retry_after=30.0)
            return _quote(1.0)

        monkeypatch.setattr(steamapis_api, "fetch_csfloat_price", fake_fetch)

        result = tradeup_buys.survey_cheapest_tradeup_buys(session, top_n_per_group=3)

        assert result.error is not None
        assert "rate limit" in result.error.lower()
        # Whichever group was processed first before the error kept its data.
        priced_ids = {c.skin.id for g in result.groups for c in g.candidates}
        assert priced_ids
        assert "in-d" not in priced_ids

        # And it's actually durable -- committed to the DB / disk, not rolled back.
        for skin_id in priced_ids:
            assert session.get(Skin, skin_id).last_price is not None

    def test_no_eligible_groups_returns_empty_result(self, session):
        result = tradeup_buys.survey_cheapest_tradeup_buys(session)
        assert result.groups == []
        assert result.error is None
        assert result.skins_priced == 0
