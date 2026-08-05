from datetime import datetime

from braindamage.contracts import filter_contracts, is_calculable
from braindamage.models import Contract


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
