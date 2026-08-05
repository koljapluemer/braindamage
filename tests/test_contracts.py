from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import signals, tradeup
from braindamage.contracts import filter_contracts, is_calculable, resimulate, upsert_contract
from braindamage.models import Base, Contract, Skin


def _make_contract(
    *,
    id: str,
    expected_value: float = 1.0,
    input_cost: float = 10.0,
    missing_input_price_names: list | None = None,
    missing_output_price_names: list | None = None,
) -> Contract:
    now = datetime.now()
    return Contract(
        id=id,
        rarity_name="Mil-Spec",
        target_rarity_name="Restricted",
        stattrak=False,
        input_cost=input_cost,
        expected_output_value=input_cost + expected_value,
        expected_value=expected_value,
        roi=None,
        cvar_5pct=None,
        favorite=False,
        created_at=now,
        last_simulated_at=now,
        input_lines=[],
        outcomes=[],
        missing_input_price_names=missing_input_price_names or [],
        missing_output_price_names=missing_output_price_names or [],
    )


class TestIsCalculable:
    def test_true_when_no_prices_missing(self):
        assert is_calculable(_make_contract(id="a")) is True

    def test_false_when_input_price_missing(self):
        contract = _make_contract(id="a", missing_input_price_names=["Some Skin (Field-Tested)"])
        assert is_calculable(contract) is False

    def test_false_when_output_price_missing(self):
        contract = _make_contract(id="a", missing_output_price_names=["Some Skin (Field-Tested)"])
        assert is_calculable(contract) is False


class TestFilterContracts:
    def test_hides_negative_ev_by_default(self):
        good = _make_contract(id="good", expected_value=5.0)
        bad = _make_contract(id="bad", expected_value=-5.0)
        assert filter_contracts([good, bad]) == [good]

    def test_hides_uncalculable_by_default(self):
        complete = _make_contract(id="complete")
        incomplete = _make_contract(id="incomplete", missing_input_price_names=["X"])
        assert filter_contracts([complete, incomplete]) == [complete]

    def test_zero_max_cost_means_no_limit(self):
        cheap = _make_contract(id="cheap", input_cost=1.0)
        expensive = _make_contract(id="expensive", input_cost=10_000.0)
        result = filter_contracts([cheap, expensive], max_cost=0.0)
        assert result == [cheap, expensive]

    def test_positive_max_cost_caps_input_cost(self):
        cheap = _make_contract(id="cheap", input_cost=1.0)
        expensive = _make_contract(id="expensive", input_cost=10_000.0)
        result = filter_contracts([cheap, expensive], max_cost=5.0)
        assert result == [cheap]

    def test_all_filters_can_be_disabled(self):
        bad_and_incomplete = _make_contract(
            id="a", expected_value=-1.0, missing_input_price_names=["X"], input_cost=999.0
        )
        result = filter_contracts(
            [bad_and_incomplete],
            hide_bad_trades=False,
            hide_uncalculable_trades=False,
            max_cost=0.0,
        )
        assert result == [bad_and_incomplete]


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


def _make_skin(session: Session, *, id: str, name: str, rarity_name: str) -> Skin:
    skin = Skin(
        id=id, name=name, category_name="Rifle", rarity_name=rarity_name,
        min_float=0.0, max_float=1.0, stattrak=False, souvenir=False,
        collection_id="col-a", collection_name="Collection A",
    )
    session.add(skin)
    session.flush()
    return skin


class TestUpsertContractEvCurve:
    """upsert_contract must persist a fresh ev_curve on every simulation, not
    leave the detail dialog to compute it ad hoc when it renders."""

    def _contract_state(self, input_skin: Skin) -> tradeup.ContractState:
        return tradeup.ContractState(
            rarity_name="Mil-Spec Grade",
            stattrak=False,
            lines=[
                tradeup.ContractLine(
                    skin_id=input_skin.id, skin_name=input_skin.name,
                    collection_id="col-a", collection_name="Collection A",
                    float_value=0.5, quantity=10,
                )
            ],
        )

    def test_upsert_populates_ev_curve(self, session, signals_dir):
        input_skin = _make_skin(session, id="in-a", name="Input", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output", rarity_name="Restricted")
        signals.append_price_observations(
            "in-a",
            [signals.PriceObservationSignal(source="cs2cap", wear_name="Field-Tested", price=1.0, fetched_at=datetime(2026, 1, 1))],
        )
        contract_state = self._contract_state(input_skin)
        result = tradeup.simulate_contract(session, contract_state)

        row = upsert_contract(session, contract_state, result)

        assert len(row.ev_curve) == 100
        assert row.ev_curve[0]["avg_float"] == pytest.approx(0.0)
        assert row.ev_curve[-1]["avg_float"] == pytest.approx(1.0)
        assert all(
            {"avg_float", "input_cost", "expected_revenue", "expected_value", "stdev"} == set(point)
            for point in row.ev_curve
        )

    def test_resimulate_refreshes_ev_curve_after_price_change(self, session, signals_dir):
        input_skin = _make_skin(session, id="in-a", name="Input", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output", rarity_name="Restricted")
        signals.append_price_observations(
            "in-a",
            [signals.PriceObservationSignal(source="cs2cap", wear_name="Factory New", price=1.0, fetched_at=datetime(2026, 1, 1))],
        )
        contract_state = self._contract_state(input_skin)
        result = tradeup.simulate_contract(session, contract_state)
        row = upsert_contract(session, contract_state, result)
        before = row.ev_curve[0]["input_cost"]  # avg_float sample 0.0 -> Factory New -> $1 * 10

        signals.append_price_observations(
            "in-a",
            [signals.PriceObservationSignal(source="cs2cap", wear_name="Factory New", price=9.0, fetched_at=datetime(2026, 1, 2))],
        )
        refreshed = resimulate(session, row)

        after = refreshed.ev_curve[0]["input_cost"]
        assert before == pytest.approx(10.0)
        assert after == pytest.approx(90.0)
