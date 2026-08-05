from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import pricing, signals
from braindamage.models import Base, Skin
from braindamage.tradeup import (
    INPUT_RARITIES,
    RARITY_LADDER,
    ContractLine,
    ContractState,
    average_float,
    average_float_achievable_range,
    collection_probability,
    cvar,
    eligible_input_skins,
    next_rarity,
    output_float,
    rarity_rank,
    simulate_contract,
    simulate_ev_curve,
    wear_for_float,
)


def _line(float_value: float, quantity: int) -> ContractLine:
    return ContractLine(
        skin_id="skin",
        skin_name="Test Skin",
        collection_id="col",
        collection_name="Test Collection",
        float_value=float_value,
        quantity=quantity,
    )


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


class TestWearForFloat:
    def test_boundaries(self):
        assert wear_for_float(0.0) == "Factory New"
        assert wear_for_float(0.069) == "Factory New"
        assert wear_for_float(0.07) == "Factory New"
        assert wear_for_float(0.071) == "Minimal Wear"
        assert wear_for_float(0.15) == "Minimal Wear"
        assert wear_for_float(0.151) == "Field-Tested"
        assert wear_for_float(0.38) == "Field-Tested"
        assert wear_for_float(0.381) == "Well-Worn"
        assert wear_for_float(0.45) == "Well-Worn"
        assert wear_for_float(0.451) == "Battle-Scarred"
        assert wear_for_float(1.0) == "Battle-Scarred"

    def test_clamps_out_of_range(self):
        assert wear_for_float(-0.5) == "Factory New"
        assert wear_for_float(1.5) == "Battle-Scarred"


class TestOutputFloat:
    def test_matches_formula(self):
        # outFloat = minOut + (maxOut - minOut) * avgInputFloat
        assert output_float(0.5, 0.0, 1.0) == pytest.approx(0.5)
        assert output_float(0.5, 0.1, 0.7) == pytest.approx(0.4)
        assert output_float(0.0, 0.06, 0.8) == pytest.approx(0.06)
        assert output_float(1.0, 0.06, 0.8) == pytest.approx(0.8)


class TestAverageFloat:
    def test_single_line_all_ten(self):
        assert average_float([_line(0.2, 10)]) == pytest.approx(0.2)

    def test_weighted_by_quantity(self):
        lines = [_line(0.1, 7), _line(0.5, 3)]
        # (0.1*7 + 0.5*3) / 10 = (0.7 + 1.5) / 10 = 0.22
        assert average_float(lines) == pytest.approx(0.22)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            average_float([])


class TestRarityLadder:
    def test_next_rarity_progression(self):
        assert next_rarity("Consumer Grade") == "Industrial Grade"
        assert next_rarity("Industrial Grade") == "Mil-Spec Grade"
        assert next_rarity("Mil-Spec Grade") == "Restricted"
        assert next_rarity("Restricted") == "Classified"
        assert next_rarity("Classified") == "Covert"

    def test_covert_has_no_next(self):
        assert next_rarity("Covert") is None

    def test_unknown_rarity_has_no_next(self):
        assert next_rarity("Contraband") is None

    def test_covert_excluded_from_input_rarities(self):
        assert "Covert" not in INPUT_RARITIES
        assert len(INPUT_RARITIES) == len(RARITY_LADDER) - 1

    def test_rarity_rank_orders_ladder(self):
        ranks = [rarity_rank(color) for _, color in RARITY_LADDER]
        assert ranks == list(range(len(RARITY_LADDER)))

    def test_rarity_rank_unknown_color(self):
        assert rarity_rank("#e4ae39") is None  # Contraband gold — not on the ladder
        assert rarity_rank(None) is None


class TestCollectionProbability:
    def test_matches_wiki_validation_example(self):
        # 8 inputs from Collection A (4 possible outputs), 2 from B (3 outputs).
        prob_a = collection_probability(8, 4)
        prob_b = collection_probability(2, 3)
        assert prob_a == pytest.approx(0.20)
        assert prob_b == pytest.approx(2 / 30)

        total = 4 * prob_a + 3 * prob_b
        assert total == pytest.approx(1.0)

    def test_zero_outputs_raises(self):
        with pytest.raises(ValueError):
            collection_probability(5, 0)


class TestCvar:
    def test_worst_single_outcome_dominates_tail(self):
        # Worst outcome alone (10%) already exceeds the 5% alpha slice, so CVaR
        # is just that outcome's profit, not blended with the next-worst one.
        outcomes = [(-100.0, 0.10), (-10.0, 0.20), (50.0, 0.70)]
        assert cvar(outcomes, alpha=0.05) == pytest.approx(-100.0)

    def test_interpolates_across_the_alpha_cutoff(self):
        # Worst outcome (-100) covers only 4% of the requested 5%; the
        # remaining 1% comes from the next-worst (-10), proportionally.
        outcomes = [(-100.0, 0.04), (-10.0, 0.20), (50.0, 0.76)]
        expected = (-100.0 * 0.04 + -10.0 * 0.01) / 0.05
        assert cvar(outcomes, alpha=0.05) == pytest.approx(expected)

    def test_alpha_covers_entire_distribution(self):
        outcomes = [(-20.0, 0.5), (20.0, 0.5)]
        assert cvar(outcomes, alpha=1.0) == pytest.approx(0.0)

    def test_no_probability_mass_returns_none(self):
        assert cvar([], alpha=0.05) is None


class TestPricingSignals:
    """Price resolution now reads a skin's JSON signal files instead of a SQL
    table — see braindamage/signals.py and braindamage/pricing.py."""

    def test_latest_price_for_wear_picks_most_recent_observation(self, signals_dir):
        signals.append_price_observations(
            "skin-a",
            [
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Field-Tested", price=10.0,
                    fetched_at=datetime(2026, 1, 1),
                ),
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Field-Tested", price=12.0,
                    fetched_at=datetime(2026, 1, 2),
                ),
                # Different wear — must not be picked for a Field-Tested lookup.
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Factory New", price=99.0,
                    fetched_at=datetime(2026, 1, 3),
                ),
            ],
        )

        result = pricing.latest_price_for_wear("skin-a", "Field-Tested")
        assert result is not None
        price, observed_at = result
        assert price == pytest.approx(12.0)
        assert observed_at == datetime(2026, 1, 2)

    def test_latest_price_for_wear_missing_data_returns_none(self, signals_dir):
        assert pricing.latest_price_for_wear("no-such-skin", "Field-Tested") is None

    def test_latest_prices_by_wear_groups_every_wear_from_one_read(self, signals_dir):
        signals.append_price_observations(
            "skin-a",
            [
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Factory New", price=10.0,
                    fetched_at=datetime(2026, 1, 1),
                ),
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Factory New", price=12.0,
                    fetched_at=datetime(2026, 1, 2),
                ),
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Battle-Scarred", price=3.0,
                    fetched_at=datetime(2026, 1, 1),
                ),
            ],
        )

        result = pricing.latest_prices_by_wear("skin-a")

        assert set(result.keys()) == {"Factory New", "Battle-Scarred"}
        assert result["Factory New"] == (12.0, datetime(2026, 1, 2))
        assert result["Battle-Scarred"] == (3.0, datetime(2026, 1, 1))

    def test_latest_prices_by_wear_missing_data_returns_empty_dict(self, signals_dir):
        assert pricing.latest_prices_by_wear("no-such-skin") == {}

    def test_recalculate_last_price_uses_latest_across_all_wears(self, session, signals_dir):
        skin = _make_skin(session, id="skin-b", name="Test Skin", rarity_name="Restricted")
        signals.append_price_observations(
            "skin-b",
            [
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Factory New", price=20.0,
                    fetched_at=datetime(2026, 1, 1),
                ),
                signals.PriceObservationSignal(
                    source="cs2cap", wear_name="Battle-Scarred", price=5.0,
                    fetched_at=datetime(2026, 1, 5),
                ),
            ],
        )

        pricing.recalculate_last_price(skin)

        assert skin.last_price == pytest.approx(5.0)
        assert skin.last_price_calculation_data_point_recency == datetime(2026, 1, 5)
        assert skin.last_price_recalculated_at is not None

    def test_recalculate_last_price_with_no_signals_clears_price(self, session, signals_dir):
        skin = _make_skin(session, id="skin-c", name="Test Skin", rarity_name="Restricted")
        skin.last_price = 42.0

        pricing.recalculate_last_price(skin)

        assert skin.last_price is None
        assert skin.last_price_calculation_data_point_recency is None
        assert skin.last_price_recalculated_at is not None


class TestEligibleInputSkins:
    def test_requires_next_rarity_output_in_same_collection(self, session):
        _make_skin(session, id="in-1", name="Eligible Input", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-1", name="Output", rarity_name="Restricted")
        # A dead-end collection: has a Mil-Spec skin but no Restricted output.
        _make_skin(
            session, id="in-2", name="Dead End Input", rarity_name="Mil-Spec Grade",
            collection_id="col-b", collection_name="Collection B",
        )

        results = eligible_input_skins(session, "Mil-Spec Grade", stattrak=False)

        assert [s.id for s in results] == ["in-1"]

    def test_stattrak_pools_dont_cross_with_normal(self, session):
        _make_skin(session, id="in-1", name="Normal Input", rarity_name="Mil-Spec Grade", stattrak=False)
        _make_skin(session, id="out-1", name="Normal Output", rarity_name="Restricted", stattrak=False)
        _make_skin(session, id="in-st", name="ST Input", rarity_name="Mil-Spec Grade", stattrak=True)
        _make_skin(session, id="out-st", name="ST Output", rarity_name="Restricted", stattrak=True)

        normal_results = eligible_input_skins(session, "Mil-Spec Grade", stattrak=False)
        stattrak_results = eligible_input_skins(session, "Mil-Spec Grade", stattrak=True)

        assert [s.id for s in normal_results] == ["in-1"]
        assert [s.id for s in stattrak_results] == ["in-st"]

    def test_knives_and_gloves_excluded(self, session):
        _make_skin(session, id="knife-1", name="Knife", rarity_name="Mil-Spec Grade", category_name="Knives")
        _make_skin(session, id="out-1", name="Output", rarity_name="Restricted", category_name="Knives")

        assert eligible_input_skins(session, "Mil-Spec Grade", stattrak=False) == []


class TestSimulateContract:
    def test_end_to_end_ev_and_roi(self, session, signals_dir):
        input_skin = _make_skin(
            session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade",
            min_float=0.0, max_float=1.0,
        )
        output_skin = _make_skin(
            session, id="output-skin", name="Output Skin", rarity_name="Restricted",
            min_float=0.0, max_float=1.0,
        )

        # avg_float = 0.5 -> output_float = 0.5 -> wear_for_float(0.5) == "Battle-Scarred"
        # for both the input lookup and the (only) possible output.
        signals.append_price_observations(
            input_skin.id,
            [signals.PriceObservationSignal(source="cs2cap", wear_name="Battle-Scarred", price=10.0, fetched_at=datetime(2026, 1, 1))],
        )
        signals.append_price_observations(
            output_skin.id,
            [signals.PriceObservationSignal(source="cs2cap", wear_name="Battle-Scarred", price=50.0, fetched_at=datetime(2026, 1, 1))],
        )

        contract = ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                ContractLine(
                    skin_id=input_skin.id, skin_name=input_skin.name,
                    collection_id="col-a", collection_name="Collection A",
                    float_value=0.5, quantity=10,
                )
            ],
        )

        result = simulate_contract(session, contract)

        assert result.input_cost == pytest.approx(100.0)  # 10 * $10
        assert len(result.outcomes) == 1
        outcome = result.outcomes[0]
        assert outcome.skin_id == output_skin.id
        assert outcome.probability == pytest.approx(1.0)  # only collection, only output
        assert outcome.predicted_wear == "Battle-Scarred"
        assert outcome.gross_price == pytest.approx(50.0)
        assert outcome.net_price == pytest.approx(50.0 * 0.85)
        assert result.expected_output_value == pytest.approx(50.0)
        assert result.expected_value == pytest.approx(50.0 * 0.85 - 100.0)
        assert result.roi == pytest.approx((50.0 * 0.85 - 100.0) / 100.0)
        assert result.missing_input_price_names == []
        assert result.missing_output_price_names == []

    def test_missing_price_is_tracked_not_raised(self, session, signals_dir):
        input_skin = _make_skin(session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="output-skin", name="Output Skin", rarity_name="Restricted")
        # No price signals written at all.

        contract = ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                ContractLine(
                    skin_id=input_skin.id, skin_name=input_skin.name,
                    collection_id="col-a", collection_name="Collection A",
                    float_value=0.5, quantity=10,
                )
            ],
        )

        result = simulate_contract(session, contract)

        assert result.input_cost == pytest.approx(0.0)
        assert result.missing_input_price_names == ["Input Skin (Battle-Scarred)"]
        assert result.missing_output_price_names == ["Output Skin (Battle-Scarred)"]
        assert result.outcomes[0].gross_price is None
        assert result.outcomes[0].net_price is None


class TestAverageFloatAchievableRange:
    def test_single_skin_matches_its_own_bounds(self, session):
        skin = _make_skin(session, id="in-1", name="Input", rarity_name="Mil-Spec Grade", min_float=0.1, max_float=0.9)
        contract = ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                ContractLine(
                    skin_id=skin.id, skin_name=skin.name, collection_id="col-a",
                    collection_name="Collection A", float_value=0.5, quantity=10,
                )
            ],
        )
        assert average_float_achievable_range(session, contract) == pytest.approx((0.1, 0.9))

    def test_weighted_across_distinct_skins(self, session):
        narrow = _make_skin(session, id="in-1", name="Narrow", rarity_name="Mil-Spec Grade", min_float=0.0, max_float=0.2)
        wide = _make_skin(session, id="in-2", name="Wide", rarity_name="Mil-Spec Grade", min_float=0.0, max_float=1.0)
        contract = ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                ContractLine(
                    skin_id=narrow.id, skin_name=narrow.name, collection_id="col-a",
                    collection_name="Collection A", float_value=0.1, quantity=7,
                ),
                ContractLine(
                    skin_id=wide.id, skin_name=wide.name, collection_id="col-a",
                    collection_name="Collection A", float_value=0.1, quantity=3,
                ),
            ],
        )
        # hi = (7*0.2 + 3*1.0) / 10 = (1.4 + 3.0) / 10 = 0.44
        lo, hi = average_float_achievable_range(session, contract)
        assert lo == pytest.approx(0.0)
        assert hi == pytest.approx(0.44)


class TestSimulateEvCurve:
    def _single_line_contract(self, input_skin: Skin) -> ContractState:
        return ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                ContractLine(
                    skin_id=input_skin.id, skin_name=input_skin.name,
                    collection_id="col-a", collection_name="Collection A",
                    float_value=0.5, quantity=10,
                )
            ],
        )

    def test_samples_span_the_achievable_range(self, session, signals_dir):
        input_skin = _make_skin(session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade", min_float=0.0, max_float=1.0)
        _make_skin(session, id="output-skin", name="Output Skin", rarity_name="Restricted", min_float=0.0, max_float=1.0)
        contract = self._single_line_contract(input_skin)

        points = simulate_ev_curve(session, contract, n_samples=100)

        assert len(points) == 100
        assert points[0].avg_float == pytest.approx(0.0)
        assert points[-1].avg_float == pytest.approx(1.0)
        # monotonically increasing float samples, equally spaced
        diffs = [b.avg_float - a.avg_float for a, b in zip(points, points[1:])]
        assert all(d == pytest.approx(diffs[0]) for d in diffs)

    def test_prices_endpoints_by_wear_bucket(self, session, signals_dir):
        input_skin = _make_skin(session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade", min_float=0.0, max_float=1.0)
        output_skin = _make_skin(session, id="output-skin", name="Output Skin", rarity_name="Restricted", min_float=0.0, max_float=1.0)
        signals.append_price_observations(
            input_skin.id,
            [
                signals.PriceObservationSignal(source="cs2cap", wear_name="Factory New", price=5.0, fetched_at=datetime(2026, 1, 1)),
                signals.PriceObservationSignal(source="cs2cap", wear_name="Battle-Scarred", price=10.0, fetched_at=datetime(2026, 1, 1)),
            ],
        )
        signals.append_price_observations(
            output_skin.id,
            [
                signals.PriceObservationSignal(source="cs2cap", wear_name="Factory New", price=20.0, fetched_at=datetime(2026, 1, 1)),
                signals.PriceObservationSignal(source="cs2cap", wear_name="Battle-Scarred", price=50.0, fetched_at=datetime(2026, 1, 1)),
            ],
        )
        contract = self._single_line_contract(input_skin)

        points = simulate_ev_curve(session, contract, n_samples=100)

        fn_point = points[0]  # avg_float == 0.0 -> Factory New both sides
        assert fn_point.input_cost == pytest.approx(50.0)  # 10 * $5
        assert fn_point.expected_revenue == pytest.approx(20.0 * 0.85)
        assert fn_point.expected_value == pytest.approx(20.0 * 0.85 - 50.0)
        assert fn_point.stdev == pytest.approx(0.0)  # single possible outcome

        bs_point = points[-1]  # avg_float == 1.0 -> Battle-Scarred both sides
        assert bs_point.input_cost == pytest.approx(100.0)  # 10 * $10
        assert bs_point.expected_revenue == pytest.approx(50.0 * 0.85)
        assert bs_point.expected_value == pytest.approx(50.0 * 0.85 - 100.0)

    def test_missing_price_for_a_bucket_contributes_zero_not_an_error(self, session, signals_dir):
        input_skin = _make_skin(session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade", min_float=0.0, max_float=1.0)
        _make_skin(session, id="output-skin", name="Output Skin", rarity_name="Restricted", min_float=0.0, max_float=1.0)
        # No price signals written at all.
        contract = self._single_line_contract(input_skin)

        points = simulate_ev_curve(session, contract, n_samples=10)

        assert all(p.input_cost == pytest.approx(0.0) for p in points)
        assert all(p.expected_revenue == pytest.approx(0.0) for p in points)

    def test_uses_each_input_skins_own_float_bounds_not_full_0_1(self, session, signals_dir):
        input_skin = _make_skin(session, id="input-skin", name="Input Skin", rarity_name="Mil-Spec Grade", min_float=0.2, max_float=0.6)
        _make_skin(session, id="output-skin", name="Output Skin", rarity_name="Restricted", min_float=0.0, max_float=1.0)
        contract = self._single_line_contract(input_skin)

        points = simulate_ev_curve(session, contract, n_samples=5)

        assert points[0].avg_float == pytest.approx(0.2)
        assert points[-1].avg_float == pytest.approx(0.6)
