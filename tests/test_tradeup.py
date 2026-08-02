import pytest

from braindamage.tradeup import (
    INPUT_RARITIES,
    RARITY_LADDER,
    ContractLine,
    average_float,
    collection_probability,
    cvar,
    next_rarity,
    output_float,
    rarity_rank,
    wear_for_float,
)


def _line(float_value: float, quantity: int) -> ContractLine:
    return ContractLine(
        market_item_id="mi",
        skin_id="skin",
        skin_name="Test Skin",
        collection_id="col",
        collection_name="Test Collection",
        wear_name="Field-Tested",
        float_value=float_value,
        quantity=quantity,
    )


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
