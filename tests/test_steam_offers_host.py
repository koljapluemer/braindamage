import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from braindamage import signals, steam_offers_host
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


def _make_skin(session: Session, *, id: str, name: str, stattrak: bool = False, souvenir: bool = False, phase: str | None = None) -> Skin:
    skin = Skin(
        id=id,
        name=name,
        weapon_name="Weapon",
        pattern_name="Pattern",
        category_name="Rifle",
        rarity_name="Mil-Spec Grade",
        rarity_color=None,
        min_float=0.0,
        max_float=1.0,
        stattrak=stattrak,
        souvenir=souvenir,
        phase=phase,
        paint_index=None,
        collection_id="col-a",
        collection_name="Collection A",
        image_url=None,
    )
    session.add(skin)
    session.flush()
    return skin


def _payload(**overrides) -> dict:
    base = {
        "market_hash_name": "Input A (Field-Tested)",
        "currency": "USD",
        "offers": [{"float_value": 0.2, "pattern_seed": 7, "price": 1.23}],
    }
    base.update(overrides)
    return base


class TestHandleMessage:
    def test_valid_payload_writes_offers_and_acks(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload())

        assert reply == {"ok": True, "skin_name": "Input A", "written": 1}
        written = signals.read_steam_offers("in-a")
        assert len(written) == 1
        assert written[0].float_value == 0.2
        assert written[0].pattern_seed == 7
        assert written[0].price == 1.23
        assert written[0].wear_name == "Field-Tested"

    def test_per_offer_wear_name_overrides_market_hash_name_wear(self, session):
        # A single Steam Market page can list every wear condition of one
        # weapon together -- market_hash_name is only ONE representative
        # card, so each offer's own wear_name (when present) must win.
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session,
            _payload(
                market_hash_name="Input A (Field-Tested)",
                offers=[
                    {"wear_name": "Battle-Scarred", "float_value": 0.9, "pattern_seed": 1, "price": 1.0},
                    {"float_value": 0.2, "pattern_seed": 2, "price": 2.0},  # no wear_name -- falls back
                ],
            ),
        )

        assert reply == {"ok": True, "skin_name": "Input A", "written": 2}
        written = {o.pattern_seed: o.wear_name for o in signals.read_steam_offers("in-a")}
        assert written == {1: "Battle-Scarred", 2: "Field-Tested"}

    def test_empty_offers_list_is_not_an_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(offers=[]))

        assert reply == {"ok": True, "skin_name": "Input A", "written": 0}

    def test_unknown_skin_returns_error(self, session):
        reply = steam_offers_host.handle_message(session, _payload())

        assert reply["ok"] is False
        assert "No matching skin" in reply["error"]

    def test_ambiguous_doppler_phase_collision_returns_error(self, session):
        _make_skin(session, id="in-a-phase-1", name="Glock-18 | Gamma Doppler", phase="Phase 1")
        _make_skin(session, id="in-a-phase-2", name="Glock-18 | Gamma Doppler", phase="Phase 2")

        reply = steam_offers_host.handle_message(
            session, _payload(market_hash_name="Glock-18 | Gamma Doppler (Field-Tested)")
        )

        assert reply["ok"] is False
        assert "Ambiguous" in reply["error"]
        assert signals.read_steam_offers("in-a-phase-1") == []
        assert signals.read_steam_offers("in-a-phase-2") == []

    def test_unsupported_currency_rejected_before_db_lookup(self, session):
        # No matching skin exists at all -- if this returned the "no matching
        # skin" error instead, the currency check wouldn't be happening first.
        reply = steam_offers_host.handle_message(session, _payload(currency="GBP"))

        assert reply["ok"] is False
        assert "GBP" in reply["error"]

    def test_eur_without_configured_rate_returns_error(self, session, monkeypatch):
        monkeypatch.setattr(steam_offers_host.config, "EUR_USD_RATE", None)
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(currency="EUR"))

        assert reply["ok"] is False
        assert "EUR_USD_RATE" in reply["error"]

    def test_eur_payload_is_converted_to_usd(self, session, monkeypatch):
        monkeypatch.setattr(steam_offers_host.config, "EUR_USD_RATE", 1.1)
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session, _payload(currency="EUR", offers=[{"float_value": 0.2, "pattern_seed": 7, "price": 1.0}])
        )

        assert reply == {"ok": True, "skin_name": "Input A", "written": 1}
        written = signals.read_steam_offers("in-a")
        assert written[0].price == pytest.approx(1.1)
        assert written[0].currency == "USD"
        assert written[0].raw == {"original_currency": "EUR", "original_price": 1.0, "eur_usd_rate": 1.1}

    def test_no_wear_suffix_returns_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(market_hash_name="Input A"))

        assert reply["ok"] is False

    def test_malformed_payload_returns_error_not_exception(self, session):
        reply = steam_offers_host.handle_message(session, {"currency": "USD"})

        assert reply["ok"] is False
        assert "Malformed" in reply["error"]


class TestMessageFraming:
    def test_write_then_read_round_trips(self):
        import io

        stream = io.BytesIO()
        steam_offers_host._write_message(stream, {"ok": True, "written": 3})
        stream.seek(0)

        result = steam_offers_host._read_message(stream)

        assert result == {"ok": True, "written": 3}

    def test_read_returns_none_on_closed_stream(self):
        import io

        assert steam_offers_host._read_message(io.BytesIO(b"")) is None
