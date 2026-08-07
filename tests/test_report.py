from datetime import datetime

import pytest

from braindamage import report
from braindamage.models import Contract
from braindamage.tradeup import RangeDetail, RangeInputDetail, RangeOutcomeDetail


def _outcome(net_price: float | None, probability: float = 1.0) -> RangeOutcomeDetail:
    return RangeOutcomeDetail(
        skin_id="out-a",
        skin_name="Output",
        collection_name="Collection A",
        probability=probability,
        predicted_wear="Field-Tested",
        predicted_float_low=0.2,
        predicted_float_high=0.2,
        gross_price=None if net_price is None else net_price / 0.85,
        net_price=net_price,
        contribution=(net_price or 0.0) * probability,
    )


def _detail(*, input_cost: float, outcomes: list[RangeOutcomeDetail]) -> RangeDetail:
    expected_revenue = sum(o.contribution for o in outcomes)
    return RangeDetail(
        inputs=[RangeInputDetail("in-a", "Input", "Field-Tested", input_cost / 10, 10, input_cost)],
        outcomes=outcomes,
        input_cost=input_cost,
        expected_revenue=expected_revenue,
        expected_value=expected_revenue - input_cost,
        worst_profit=min((o.net_price if o.net_price is not None else 0.0) - input_cost for o in outcomes),
        profit_chance=sum(
            o.probability for o in outcomes if (o.net_price if o.net_price is not None else 0.0) - input_cost > 0
        ),
    )


class TestRepricedWithRealCost:
    def test_recomputes_ev_worst_case_and_profit_chance_from_real_cost(self):
        detail = _detail(input_cost=50.0, outcomes=[_outcome(100.0)])

        repriced = report._repriced_with_real_cost(detail, real_input_cost=60.0)

        assert repriced.input_cost == 60.0
        assert repriced.expected_value == pytest.approx(40.0)  # 100 - 60
        assert repriced.worst_profit == pytest.approx(40.0)
        assert repriced.profit_chance == pytest.approx(1.0)
        # Revenue side is untouched -- it's assumed already fresh (postvalidation's
        # own CSFloat output-ask refresh), only the cost side is being overridden.
        assert repriced.expected_revenue == detail.expected_revenue
        assert repriced.outcomes == detail.outcomes


class TestApplyPostvalidation:
    def _range_evals(self):
        r = {"min_float": 0.15, "max_float": 0.38}
        return [(r, _detail(input_cost=50.0, outcomes=[_outcome(100.0)]))]

    def test_drops_unexecutable_range(self):
        pv = [
            {
                "min_float": 0.15, "max_float": 0.38,
                "listings_found": 3, "executable": False, "real_input_cost": None, "checked_at": "x",
            }
        ]
        assert report._apply_postvalidation(self._range_evals(), pv) == []

    def test_drops_negative_real_ev_range(self):
        pv = [
            {
                "min_float": 0.15, "max_float": 0.38,
                "listings_found": 10, "executable": True, "real_input_cost": 150.0, "checked_at": "x",
            }
        ]
        assert report._apply_postvalidation(self._range_evals(), pv) == []

    def test_drops_range_with_no_matching_postvalidation_entry(self):
        pv = [
            {
                "min_float": 0.90, "max_float": 0.99,
                "listings_found": 10, "executable": True, "real_input_cost": 10.0, "checked_at": "x",
            }
        ]
        assert report._apply_postvalidation(self._range_evals(), pv) == []

    def test_keeps_and_reprices_executable_positive_ev_range(self):
        pv = [
            {
                "min_float": 0.15, "max_float": 0.38,
                "listings_found": 10, "executable": True, "real_input_cost": 60.0, "checked_at": "2026-01-01",
            }
        ]
        result = report._apply_postvalidation(self._range_evals(), pv)
        assert len(result) == 1
        r, detail = result[0]
        assert detail.input_cost == 60.0
        assert detail.expected_value == pytest.approx(40.0)
        assert r["postvalidation"]["real_input_cost"] == 60.0


class TestFilterPostvalidated:
    def _contract(self, id_: str, postvalidated_ranges: list[dict]) -> Contract:
        now = datetime.now()
        return Contract(
            id=id_, rarity_name="Mil-Spec", target_rarity_name="Restricted", stattrak=False,
            input_cost=10.0, expected_output_value=20.0, expected_value=10.0, roi=None, cvar_5pct=None,
            favorite=False, created_at=now, last_simulated_at=now,
            input_lines=[], outcomes=[], missing_input_price_names=[], missing_output_price_names=[],
            postvalidated_ranges=postvalidated_ranges,
        )

    def test_drops_contracts_with_no_surviving_range_and_keeps_the_rest(self, monkeypatch):
        good_pv = [
            {
                "min_float": 0.15, "max_float": 0.38,
                "listings_found": 10, "executable": True, "real_input_cost": 60.0, "checked_at": "x",
            }
        ]
        bad_pv = [
            {
                "min_float": 0.15, "max_float": 0.38,
                "listings_found": 3, "executable": False, "real_input_cost": None, "checked_at": "x",
            }
        ]
        good = self._contract("good", good_pv)
        bad = self._contract("bad", bad_pv)
        never_checked = self._contract("never", [])

        r = {"min_float": 0.15, "max_float": 0.38}
        fake_detail = _detail(input_cost=50.0, outcomes=[_outcome(100.0)])
        monkeypatch.setattr(report, "evaluate_ranges", lambda c, session: [(r, fake_detail)])

        selection = report.Selection(
            contracts=[good, bad, never_checked],
            total_generated=3, top_ev_pct_count=3, top_net_win_count=3, positive_cvar_count=0,
        )

        result = report.filter_postvalidated(selection, session=None)

        assert [c.id for c in result.contracts] == ["good"]
        # Everything else about the selection is left as-is -- only .contracts shrinks.
        assert result.total_generated == 3
