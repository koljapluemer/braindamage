from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import contracts as contracts_module
from braindamage import signals, steam_fees, steam_market_api, tradeup
from braindamage.models import Base, Contract, Skin


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


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"success": True, "median_price": "$41.93", "lowest_price": "$40.00"}, 41.93),
        ({"success": True, "lowest_price": "$1,234.56"}, 1234.56),  # median missing -> falls back
        ({"success": False, "median_price": "$41.93"}, None),  # unsuccessful -> ignored
        ({"success": True}, None),  # no price fields at all
    ],
)
def test_median_price(response, expected):
    result = steam_market_api._median_price(response)
    assert result == (pytest.approx(expected) if expected is not None else None)


class TestRefreshContractPrices:
    def _setup_mono_contract(self, session: Session, monkeypatch) -> Contract:
        monkeypatch.setattr(steam_market_api, "REQUEST_INTERVAL_SECONDS", 0.0)
        _make_skin(session, id="in-a", name="Cheap Input", rarity_name="Mil-Spec Grade")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        signals.append_price_observations(
            "in-a",
            [signals.PriceObservationSignal(
                source="steam_priceoverview", wear_name="Field-Tested", price=1.0,
                fetched_at=datetime(2020, 1, 1),
            )],
        )
        contract_state = tradeup.ContractState(
            rarity_name="Mil-Spec Grade", stattrak=False,
            lines=[tradeup.ContractLine(
                skin_id="in-a", skin_name="Cheap Input", collection_id="col-a",
                collection_name="Collection A", float_value=0.2, quantity=10,
            )],
        )
        result = tradeup.simulate_contract(session, contract_state)
        return contracts_module.upsert_contract(session, contract_state, result)

    def test_refetches_prices_and_resimulates_contract(self, session, monkeypatch):
        contract = self._setup_mono_contract(session, monkeypatch)
        assert contract.input_cost == pytest.approx(10.0)  # stale $1.0 price

        def fake_fetch(name: str) -> dict:
            price = "$2.00" if "Cheap Input" in name else "$50.00" if "Output A" in name else None
            return {"success": price is not None, "median_price": price}

        monkeypatch.setattr(steam_market_api, "_fetch_price_overview", fake_fetch)

        refresh_result = steam_market_api.refresh_contract_prices(session, contract)

        assert refresh_result == steam_market_api.SteamPriceRefreshResult(
            contract_id=contract.id, requests_made=10, observations=10,
            wears_not_found=0, skins_updated=2, error=None,
        )
        updated = session.get(Contract, contract.id)
        assert updated.input_cost == pytest.approx(20.0)  # 10 x $2.00
        assert updated.expected_value == pytest.approx(steam_fees.net_proceeds(50.0) - 20.0)

    def test_gives_up_after_exhausting_retries_on_persistent_429(self, session, monkeypatch):
        contract = self._setup_mono_contract(session, monkeypatch)
        monkeypatch.setattr(steam_market_api.time, "sleep", lambda _seconds: None)

        calls = {"count": 0}

        def flaky_fetch(name: str) -> dict:
            calls["count"] += 1
            if calls["count"] >= 3:
                raise steam_market_api.SteamMarketAPIError(429, "rate limited")
            return {"success": True, "median_price": "$2.00"}

        monkeypatch.setattr(steam_market_api, "_fetch_price_overview", flaky_fetch)

        refresh_result = steam_market_api.refresh_contract_prices(session, contract)

        assert refresh_result.error == "rate limited"
        assert refresh_result.observations == 2  # the two successful calls before the failure
        assert refresh_result.skins_updated == 1  # that skin's partial observations were still saved
