import pytest

from braindamage import steam_fees


class TestNetProceeds:
    def test_zero_or_negative_is_zero(self):
        assert steam_fees.net_proceeds(0.0) == 0.0
        assert steam_fees.net_proceeds(-5.0) == 0.0

    def test_three_cent_listing_nets_one_cent(self):
        # Widely-cited edge case: at the very bottom of the price range, the
        # $0.01 minimum on *each* of the two independent fees dominates --
        # a $0.03 sale nets $0.01, not $0.03 * (1 - 0.15) = $0.0255.
        assert steam_fees.net_proceeds(0.03) == pytest.approx(0.01)

    def test_two_cent_listing_nets_nothing(self):
        # Steam's fee minimums alone ($0.01 Steam + $0.01 game) already eat
        # the entire $0.02 -- the cheapest possible listing that still nets
        # the seller anything is $0.03.
        assert steam_fees.net_proceeds(0.02) == pytest.approx(0.0)

    def test_hundred_dollar_listing(self):
        # The two cuts are computed on top of the net amount, not off the
        # gross -- so the net fraction is ~1/1.15 (~87%), not the naive
        # (1 - 0.05 - 0.10) = 0.85 this codebase used to assume.
        assert steam_fees.net_proceeds(100.0) == pytest.approx(86.97)

    def test_ten_dollar_listing(self):
        assert steam_fees.net_proceeds(10.0) == pytest.approx(8.70)

    def test_result_round_trips_through_the_real_buyer_pays_formula(self):
        # For any gross price this produces, re-deriving buyer-pays from
        # that net amount via the forward (non-iterative) formula must land
        # back on the exact original gross price -- that's the actual
        # correctness property (CalculateAmountToSendForDesiredReceivedAmount
        # of CalculateFeeAmount's output reproduces the input), independent
        # of any specific hand-picked example.
        for cents in [3, 4, 17, 99, 100, 250, 1000, 10000, 999999]:
            net_cents = steam_fees.net_proceeds_cents(cents)
            _steam_fee, _publisher_fee, rebuilt = steam_fees._amount_to_send_cents(
                net_cents, steam_fees.CS_PUBLISHER_FEE_PERCENT
            )
            assert rebuilt == cents

    def test_no_publisher_fee_is_steam_cut_only(self):
        # publisher_fee_percent=0 -- e.g. a game with no separate game fee --
        # only the flat 5% Steam cut applies, no second $0.01 minimum.
        assert steam_fees.net_proceeds(10.0, publisher_fee_percent=0.0) == pytest.approx(9.53)
