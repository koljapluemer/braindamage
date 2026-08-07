from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import contracts as contracts_module
from braindamage import csfloat_api, postvalidate, signals, tradeup
from braindamage.models import Base, Skin


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


# Input priced cheapest-at-Battle-Scarred (identity float mapping, since both
# skins span the full [0, 1] range) so the EV curve's top-3 optimization
# ranges are exactly the Battle-Scarred / Well-Worn / Field-Tested input-wear
# bands -- see the boundary math in this module's docstring-equivalent below.
_INPUT_PRICES = {
    "Factory New": 100.0, "Minimal Wear": 50.0, "Field-Tested": 20.0,
    "Well-Worn": 10.0, "Battle-Scarred": 5.0,
}


def _setup_contract(session: Session, *, suffix: str = "a"):
    input_id, output_id = f"in-{suffix}", f"out-{suffix}"
    collection_id = f"col-{suffix}"
    input_skin = _make_skin(session, id=input_id, name=f"Input {suffix}", rarity_name="Mil-Spec Grade")
    input_skin.collection_id = collection_id
    _make_skin(session, id=output_id, name=f"Output {suffix}", rarity_name="Restricted").collection_id = (
        collection_id
    )
    session.flush()

    fetched_at = datetime(2026, 1, 1)
    signals.append_price_observations(
        input_id,
        [
            signals.PriceObservationSignal(source="cs2cap", wear_name=wear, price=price, fetched_at=fetched_at)
            for wear, price in _INPUT_PRICES.items()
        ],
    )
    signals.append_price_observations(
        output_id,
        [
            signals.PriceObservationSignal(source="cs2cap", wear_name=wear, price=200.0, fetched_at=fetched_at)
            for wear in _INPUT_PRICES
        ],
    )

    contract_state = tradeup.ContractState(
        rarity_name="Mil-Spec Grade", stattrak=False,
        lines=[tradeup.ContractLine(
            skin_id=input_id, skin_name=f"Input {suffix}", collection_id=collection_id,
            collection_name=f"Collection {suffix}", float_value=0.5, quantity=10,
        )],
    )
    result = tradeup.simulate_contract(session, contract_state)
    return contracts_module.upsert_contract(session, contract_state, result)


class TestPostvalidateContract:
    def test_records_per_range_results_and_writes_signals(self, session, signals_dir, monkeypatch):
        contract = _setup_contract(session)
        # Top-3 optimization ranges by construction: Battle-Scarred (cheapest,
        # best EV), Well-Worn, Field-Tested.
        assert len(contract.optimization_ranges) == 3

        def fake_listings(market_hash_name, min_float, max_float, limit=10):
            count = 10 if "Battle-Scarred" in market_hash_name else 4
            return [
                csfloat_api.FloatListing(f"L{i}", market_hash_name, "?", 0.5, 6.0, "buy_now", {})
                for i in range(count)
            ]

        monkeypatch.setattr(csfloat_api, "cheapest_listings_in_float_range", fake_listings)
        monkeypatch.setattr(csfloat_api, "lowest_ask", lambda market_hash_name: 250.0)

        result = postvalidate.postvalidate_contract(session, contract)

        by_bounds = {(rp.min_float, rp.max_float): rp for rp in result.ranges}
        bs_bounds = max(by_bounds, key=lambda bounds: bounds[1])  # highest max_float = Battle-Scarred
        bs_range = by_bounds[bs_bounds]
        assert bs_range.listings_found == 10
        assert bs_range.executable is True
        assert bs_range.real_input_cost == pytest.approx(60.0)  # 10 x $6

        others = [rp for bounds, rp in by_bounds.items() if bounds != bs_bounds]
        assert len(others) == 2
        for rp in others:
            assert rp.listings_found == 4
            assert rp.executable is False
            assert rp.real_input_cost == pytest.approx(24.0)  # 4 x $6

        # Persisted onto the Contract row, not just returned.
        assert len(contract.postvalidated_ranges) == 3
        assert contract.postvalidated_at is not None

        # Input-side MarketOfferSignals: 10 + 4 + 4 = 18, all from CSFloat.
        offers = signals.read_market_offers("in-a")
        assert len(offers) == 18
        assert all(o.source == "csfloat" for o in offers)

        # Output-side: each range's predicted wear differs here (Battle-Scarred /
        # Well-Worn / Field-Tested), so all 3 lowest-ask lookups are distinct and
        # each lands its own PriceObservationSignal.
        output_observations = signals.read_price_observations("out-a")
        csfloat_observations = [o for o in output_observations if o.source == "csfloat"]
        assert len(csfloat_observations) == 3
        assert all(o.price == pytest.approx(250.0) for o in csfloat_observations)

        assert result.requests_made == 6  # 3 input-listing calls + 3 output lowest-ask calls

    def test_error_partway_through_keeps_already_checked_ranges(self, session, signals_dir, monkeypatch):
        """A CSFloat failure (e.g. sustained rate limiting) on the 2nd of 3
        ranges must not discard the 1st range's already-fetched result --
        losing partial progress on every already-checked contract over one
        failed call is exactly the bug this behavior fixes."""
        contract = _setup_contract(session)
        calls = {"count": 0}

        def flaky_listings(market_hash_name, min_float, max_float, limit=10):
            calls["count"] += 1
            if calls["count"] == 1:
                return [
                    csfloat_api.FloatListing(f"L{i}", market_hash_name, "?", 0.5, 6.0, "buy_now", {})
                    for i in range(10)
                ]
            raise csfloat_api.CsfloatRateLimitError(retry_after=0.0)

        monkeypatch.setattr(csfloat_api, "cheapest_listings_in_float_range", flaky_listings)
        monkeypatch.setattr(csfloat_api, "lowest_ask", lambda market_hash_name: 250.0)

        result = postvalidate.postvalidate_contract(session, contract)

        assert result.error is not None
        assert len(result.ranges) == 1
        assert result.ranges[0].executable is True
        assert result.ranges[0].real_input_cost == pytest.approx(60.0)
        # Persisted despite the error -- partial progress isn't lost.
        assert len(contract.postvalidated_ranges) == 1
        assert contract.postvalidated_at is not None


class TestPostvalidateContracts:
    def test_one_contract_erroring_unexpectedly_does_not_stop_the_batch(self, session, signals_dir, monkeypatch):
        failing = _setup_contract(session, suffix="a")
        succeeding = _setup_contract(session, suffix="b")

        real_postvalidate_contract = postvalidate.postvalidate_contract

        def flaky(session_, contract):
            if contract.id == failing.id:
                raise csfloat_api.CsfloatAPIError(None, "boom")
            return real_postvalidate_contract(session_, contract)

        monkeypatch.setattr(postvalidate, "postvalidate_contract", flaky)
        monkeypatch.setattr(
            csfloat_api,
            "cheapest_listings_in_float_range",
            lambda market_hash_name, min_float, max_float, limit=10: [
                csfloat_api.FloatListing(f"L{i}", market_hash_name, "?", 0.5, 6.0, "buy_now", {}) for i in range(10)
            ],
        )
        monkeypatch.setattr(csfloat_api, "lowest_ask", lambda market_hash_name: 250.0)

        results = postvalidate.postvalidate_contracts(session, [failing, succeeding])

        assert len(results) == 2
        by_id = {r.contract_id: r for r in results}
        assert by_id[failing.id].error == "boom"
        assert by_id[failing.id].ranges == []
        assert by_id[succeeding.id].error is None
        assert len(by_id[succeeding.id].ranges) == 3

    def test_circuit_breaker_stops_the_batch_after_sustained_consecutive_errors(
        self, session, signals_dir, monkeypatch
    ):
        """CSFloat being sustained-unhappy (not one flaky contract) must stop
        the run rather than retry every remaining contract at the same
        degraded odds -- see the module docstring."""
        contracts = [_setup_contract(session, suffix=s) for s in "abcd"]
        monkeypatch.setattr(
            postvalidate, "postvalidate_contract",
            lambda session_, contract: (_ for _ in ()).throw(csfloat_api.CsfloatAPIError(None, "boom")),
        )

        results = postvalidate.postvalidate_contracts(session, contracts, max_consecutive_errors=2)

        assert len(results) == 2  # stopped after the 2nd consecutive error, 2 contracts left unattempted
        assert all(r.error == "boom" for r in results)

    def test_max_backoff_exceeded_stops_the_batch_immediately(self, session, signals_dir, monkeypatch):
        """A single contract hitting CsfloatMaxBackoffExceeded (backoff
        saturated at its ceiling -- see csfloat_api) should abort the rest of
        the batch right away, not just count as one more error toward
        max_consecutive_errors -- this usually means we're blocked for hours,
        so retrying the next contracts at the same odds is pointless."""
        contracts = [_setup_contract(session, suffix=s) for s in "abcd"]
        monkeypatch.setattr(
            postvalidate, "postvalidate_contract",
            lambda session_, contract: (_ for _ in ()).throw(
                csfloat_api.CsfloatMaxBackoffExceeded(retry_after=300.0)
            ),
        )

        results = postvalidate.postvalidate_contracts(session, contracts, max_consecutive_errors=10)

        assert len(results) == 1  # stopped after the very first contract, 3 left unattempted
        assert results[0].max_backoff_hit is True

    def test_overall_deadline_stops_the_batch(self, session, signals_dir, monkeypatch):
        contracts = [_setup_contract(session, suffix=s) for s in "ab"]
        monkeypatch.setattr(
            csfloat_api,
            "cheapest_listings_in_float_range",
            lambda market_hash_name, min_float, max_float, limit=10: [
                csfloat_api.FloatListing(f"L{i}", market_hash_name, "?", 0.5, 6.0, "buy_now", {}) for i in range(10)
            ],
        )
        monkeypatch.setattr(csfloat_api, "lowest_ask", lambda market_hash_name: 250.0)
        # started_at, elapsed-check before contract 1 (within budget), elapsed-check
        # before contract 2 (over budget) -- contract 1's own work makes no
        # time.monotonic() calls since the csfloat_api calls above are stubbed
        # directly, bypassing the real rate limiter.
        clock = iter([0.0, 0.0, 9999.0])
        monkeypatch.setattr(postvalidate.time, "monotonic", lambda: next(clock))

        results = postvalidate.postvalidate_contracts(session, contracts, max_total_seconds=1200.0)

        assert len(results) == 1
