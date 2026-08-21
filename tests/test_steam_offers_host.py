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


def _make_skin(
    session: Session,
    *,
    id: str,
    name: str,
    stattrak: bool = False,
    souvenir: bool = False,
    phase: str | None = None,
    rarity_name: str = "Mil-Spec Grade",
    collection_id: str = "col-a",
    collection_name: str = "Collection A",
) -> Skin:
    skin = Skin(
        id=id,
        name=name,
        weapon_name="Weapon",
        pattern_name="Pattern",
        category_name="Rifle",
        rarity_name=rarity_name,
        rarity_color=None,
        min_float=0.0,
        max_float=1.0,
        stattrak=stattrak,
        souvenir=souvenir,
        phase=phase,
        paint_index=None,
        collection_id=collection_id,
        collection_name=collection_name,
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


def _reply(skin_name: str, written: int, *, buy_order_written: bool = False) -> dict:
    # No test skin here has an eligible next-rarity output on disk, so
    # build_table always fails the same way -- matches _make_skin's defaults
    # (rarity_name="Mil-Spec Grade" -> next rarity "Restricted",
    # collection_name="Collection A").
    return {
        "ok": True,
        "skin_name": skin_name,
        "written": written,
        "buy_order_written": buy_order_written,
        "table": None,
        "table_error": "Collection A has no eligible output at 'Restricted'.",
        "float_diagrams": None,
        "contract_history": [],
    }


class TestHandleMessage:
    def test_valid_payload_writes_offers_and_acks(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload())

        assert reply == _reply("Input A", 1)
        written = signals.read_steam_offers("in-a")
        assert len(written) == 1
        assert written[0].float_value == 0.2
        assert written[0].pattern_seed == 7
        assert written[0].price == 1.23
        assert written[0].wear_name == "Field-Tested"

    def test_comprehensive_flag_is_stamped_onto_written_offers(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(comprehensive=True))

        assert reply == _reply("Input A", 1)
        written = signals.read_steam_offers("in-a")
        assert written[0].comprehensive is True

    def test_comprehensive_flag_defaults_to_false(self, session):
        _make_skin(session, id="in-a", name="Input A")

        steam_offers_host.handle_message(session, _payload())

        written = signals.read_steam_offers("in-a")
        assert written[0].comprehensive is False

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

        assert reply == _reply("Input A", 2)
        written = {o.pattern_seed: o.wear_name for o in signals.read_steam_offers("in-a")}
        assert written == {1: "Battle-Scarred", 2: "Field-Tested"}

    def test_empty_offers_list_is_not_an_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(offers=[]))

        assert reply == _reply("Input A", 0)

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

        assert reply == _reply("Input A", 1)
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

    def test_buy_order_summary_is_saved_and_flagged(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session,
            _payload(
                buy_order_summary={"wear_name": "Field-Tested", "price": 143.65, "num_orders": 2302}
            ),
        )

        assert reply == _reply("Input A", 1, buy_order_written=True)
        saved = signals.read_buy_order_summaries("in-a")
        assert len(saved) == 1
        assert saved[0].wear_name == "Field-Tested"
        assert saved[0].price == 143.65
        assert saved[0].num_orders == 2302
        assert saved[0].currency == "USD"

    def test_buy_order_summary_converts_eur_to_usd(self, session, monkeypatch):
        monkeypatch.setattr(steam_offers_host.config, "EUR_USD_RATE", 1.1)
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session,
            _payload(
                currency="EUR",
                buy_order_summary={"wear_name": "Field-Tested", "price": 100.0, "num_orders": 5},
            ),
        )

        assert reply["buy_order_written"] is True
        saved = signals.read_buy_order_summaries("in-a")
        assert saved[0].price == pytest.approx(110.0)
        assert saved[0].currency == "USD"
        assert saved[0].raw == {"original_currency": "EUR", "original_price": 100.0, "eur_usd_rate": 1.1}

    def test_missing_buy_order_summary_is_not_an_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(buy_order_summary=None))

        assert reply == _reply("Input A", 1)
        assert signals.read_buy_order_summaries("in-a") == []

    def test_reply_includes_table_when_skin_has_an_eligible_output(self, session):
        _make_skin(session, id="in-a", name="Input A")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")

        reply = steam_offers_host.handle_message(session, _payload())

        assert reply["ok"] is True
        assert reply["table_error"] is None
        assert reply["table"]["input_header"] == {
            "skin_id": "in-a",
            "skin_name": "Input A",
            # min_float=0.0/max_float=1.0 (this fixture's defaults) midpoints
            # to 0.5, which wear_for_float buckets as Battle-Scarred.
            "steam_url": "https://steamcommunity.com/market/listings/730/Input%20A%20%28Battle-Scarred%29",
        }
        assert [h["skin_name"] for h in reply["table"]["outcome_headers"]] == ["Output A"]
        assert len(reply["table"]["rows"]) == 5


def _ten_offers(start_price: float = 1.0) -> list[dict]:
    return [
        {"float_value": 0.02 + i * 0.001, "pattern_seed": i, "price": start_price + i}
        for i in range(10)
    ]


class TestHandleConstructContract:
    def test_builds_contract_from_payload_offers_only_and_still_writes_to_disk(self, session):
        _make_skin(session, id="in-a", name="Input A")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        signals.append_price_observations(
            "out-a",
            [signals.PriceObservationSignal(source="test", wear_name="Factory New", price=100.0, fetched_at=now_utc())],
        )
        # Stale disk data for the same skin that handle_construct_contract must
        # NOT draw from -- if it leaked in, the cheapest 10 would include this
        # $0.01 offer instead of matching _ten_offers()'s real_cost exactly.
        signals.append_steam_offers(
            "in-a",
            [
                signals.SteamOfferSignal(
                    market_hash_name="Input A (Field-Tested)",
                    wear_name="Field-Tested",
                    float_value=0.5,
                    pattern_seed=999,
                    price=0.01,
                    fetched_at=now_utc(),
                )
            ],
        )

        reply = steam_offers_host.handle_message(
            session, _payload(offers=_ten_offers(), action="construct_contract")
        )

        assert reply["ok"] is True
        assert reply["written"] == 10
        contract = reply["contract"]
        assert contract["skin_name"] == "Input A"
        assert contract["real_cost"] == pytest.approx(sum(1.0 + i for i in range(10)))
        assert len(contract["offers"]) == 10
        assert all(o["pattern_seed"] != 999 for o in contract["offers"])
        assert contract["outcomes"][0]["predicted_wear"] == "Factory New"
        # avg of 0.02 + i*0.001 for i in 0..9, unnormalized -- distinct from
        # avg_float, which is rescaled into the skin's own [min_float,
        # max_float] window.
        assert contract["raw_avg_float"] == pytest.approx(0.0245)

        # Still saved to disk exactly like a normal fetch would.
        on_disk = signals.read_steam_offers("in-a")
        assert len(on_disk) == 11  # the 10 fresh + the pre-seeded stale one

        # And the contract itself is recorded to the per-skin history, which
        # comes back in the same reply for the sidebar's history list.
        history = signals.read_contract_history("in-a")
        assert len(history) == 1
        assert history[0].expected_value == pytest.approx(contract["expected_value"])
        assert history[0].raw_avg_float == pytest.approx(0.0245)
        assert reply["contract_history"] == [
            {
                "generated_at": history[0].generated_at.isoformat(),
                "expected_value": history[0].expected_value,
                "raw_avg_float": history[0].raw_avg_float,
            }
        ]

    def test_contract_history_keeps_only_the_5_most_recent_newest_first(self, session):
        _make_skin(session, id="in-a", name="Input A")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        signals.append_price_observations(
            "out-a",
            [signals.PriceObservationSignal(source="test", wear_name="Factory New", price=100.0, fetched_at=now_utc())],
        )

        evs = []
        for i in range(6):
            reply = steam_offers_host.handle_message(
                session, _payload(offers=_ten_offers(start_price=1.0 + i), action="construct_contract")
            )
            assert reply["ok"] is True
            evs.append(reply["contract"]["expected_value"])

        assert len(signals.read_contract_history("in-a")) == 6
        history = reply["contract_history"]
        assert len(history) == 5
        # Newest first, and the oldest run (i=0) has aged out of the
        # returned last-5 window.
        assert [h["expected_value"] for h in history] == list(reversed(evs[1:]))

    def test_fewer_than_ten_in_window_offers_returns_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session, _payload(offers=_ten_offers()[:9], action="construct_contract")
        )

        assert reply["ok"] is False
        assert "Only 9 listing" in reply["error"]

    def test_offers_with_no_float_dont_count_toward_the_ten(self, session):
        _make_skin(session, id="in-a", name="Input A")
        offers = _ten_offers()
        offers[0] = {"pattern_seed": 0, "price": 1.0}  # no float_value

        reply = steam_offers_host.handle_message(
            session, _payload(offers=offers, action="construct_contract")
        )

        assert reply["ok"] is False
        assert "Only 9 listing" in reply["error"]

    def test_invalid_input_skin_returns_error(self, session):
        _make_skin(session, id="in-covert", name="Input Covert", rarity_name="Covert")

        reply = steam_offers_host.handle_message(
            session,
            _payload(
                market_hash_name="Input Covert (Field-Tested)",
                offers=_ten_offers(),
                action="construct_contract",
            ),
        )

        assert reply["ok"] is False
        assert "isn't a usable mono trade-up input" in reply["error"]

    def test_unknown_action_returns_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _payload(action="not_a_real_action"))

        assert reply["ok"] is False
        assert "Unknown action" in reply["error"]


def _csfloat_payload(**overrides) -> dict:
    base = {
        "action": "fetch_csfloat_offers",
        "base_skin_name": "Input A",
        "currency": "USD",
        "offers": [
            {
                "wear_name": "Field-Tested",
                "float_value": 0.2,
                "pattern_seed": 7,
                "price": 1.23,
                "stattrak": False,
                "souvenir": False,
                "listing_id": "csfloat-1",
                "listing_type": "buy_now",
            }
        ],
    }
    base.update(overrides)
    return base


class TestHandleFetchCsfloatOffers:
    def test_valid_payload_writes_market_offers_and_acks(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(session, _csfloat_payload())

        assert reply["ok"] is True
        assert reply["skin_name"] == "Input A"
        assert reply["written"] == 1
        assert reply["group_errors"] == []
        written = signals.read_market_offers("in-a")
        assert len(written) == 1
        assert written[0].source == "csfloat"
        assert written[0].listing_id == "csfloat-1"
        assert written[0].float_value == 0.2
        assert written[0].price == 1.23
        assert written[0].wear_name == "Field-Tested"
        assert written[0].listing_type == "buy_now"
        assert written[0].raw["pattern_seed"] == 7

    def test_comprehensive_flag_is_stamped_onto_written_offers(self, session):
        _make_skin(session, id="in-a", name="Input A")

        steam_offers_host.handle_message(session, _csfloat_payload(comprehensive=True))

        written = signals.read_market_offers("in-a")
        assert written[0].comprehensive is True

    def test_comprehensive_flag_defaults_to_false(self, session):
        _make_skin(session, id="in-a", name="Input A")

        steam_offers_host.handle_message(session, _csfloat_payload())

        written = signals.read_market_offers("in-a")
        assert written[0].comprehensive is False

    def test_offers_are_grouped_by_stattrak_souvenir_into_separate_skins(self, session):
        _make_skin(session, id="normal", name="Input A", stattrak=False, souvenir=False)
        _make_skin(session, id="st", name="Input A", stattrak=True, souvenir=False)

        reply = steam_offers_host.handle_message(
            session,
            _csfloat_payload(
                offers=[
                    {
                        "wear_name": "Field-Tested", "float_value": 0.2, "price": 1.0,
                        "stattrak": False, "souvenir": False, "listing_id": "l1", "listing_type": "buy_now",
                    },
                    {
                        "wear_name": "Field-Tested", "float_value": 0.3, "price": 5.0,
                        "stattrak": True, "souvenir": False, "listing_id": "l2", "listing_type": "buy_now",
                    },
                    {
                        "wear_name": "Field-Tested", "float_value": 0.31, "price": 6.0,
                        "stattrak": True, "souvenir": False, "listing_id": "l3", "listing_type": "buy_now",
                    },
                ]
            ),
        )

        assert reply["ok"] is True
        assert reply["written"] == 3
        # The StatTrak group has 2 offers vs. the normal group's 1, so it's
        # the "primary" skin the returned table/skin_name reflect.
        assert reply["skin_name"] == "Input A"
        assert len(signals.read_market_offers("normal")) == 1
        assert len(signals.read_market_offers("st")) == 2

    def test_unresolvable_group_reports_error_but_other_groups_still_save(self, session):
        _make_skin(session, id="normal", name="Input A", stattrak=False, souvenir=False)
        # No StatTrak "Input A" skin exists in the catalog.

        reply = steam_offers_host.handle_message(
            session,
            _csfloat_payload(
                offers=[
                    {
                        "wear_name": "Field-Tested", "float_value": 0.2, "price": 1.0,
                        "stattrak": False, "souvenir": False, "listing_id": "l1", "listing_type": "buy_now",
                    },
                    {
                        "wear_name": "Field-Tested", "float_value": 0.3, "price": 5.0,
                        "stattrak": True, "souvenir": False, "listing_id": "l2", "listing_type": "buy_now",
                    },
                ]
            ),
        )

        assert reply["ok"] is True
        assert reply["written"] == 1
        assert len(reply["group_errors"]) == 1
        assert "No matching skin" in reply["group_errors"][0]
        assert len(signals.read_market_offers("normal")) == 1

    def test_no_resolvable_groups_returns_error(self, session):
        reply = steam_offers_host.handle_message(session, _csfloat_payload())

        assert reply["ok"] is False
        assert "No matching skin" in reply["error"]

    def test_eur_payload_is_converted_to_usd(self, session, monkeypatch):
        monkeypatch.setattr(steam_offers_host.config, "EUR_USD_RATE", 1.1)
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session, _csfloat_payload(currency="EUR", offers=[
                {
                    "wear_name": "Field-Tested", "float_value": 0.2, "price": 1.0,
                    "stattrak": False, "souvenir": False, "listing_id": "l1", "listing_type": "buy_now",
                }
            ])
        )

        assert reply["ok"] is True
        written = signals.read_market_offers("in-a")
        assert written[0].price == pytest.approx(1.1)
        assert written[0].currency == "USD"
        assert written[0].raw["original_currency"] == "EUR"

    def test_reply_includes_table_priced_from_input_source(self, session):
        _make_skin(session, id="in-a", name="Input A")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")

        reply = steam_offers_host.handle_message(
            session, _csfloat_payload(input_source="csfloat", offers=[
                {
                    "wear_name": "Battle-Scarred", "float_value": 0.9, "price": p,
                    "stattrak": False, "souvenir": False, "listing_id": f"l{i}", "listing_type": "buy_now",
                }
                for i, p in enumerate(range(1, 11))
            ])
        )

        assert reply["ok"] is True
        assert reply["table_error"] is None
        row = next(r for r in reply["table"]["rows"] if r["wear_name"] == "Battle-Scarred")
        assert row["input_cell"]["value"] == pytest.approx(sum(range(1, 11)))

        # Same skin, but the "steam" dropdown selection sees nothing (no
        # SteamOfferSignal was ever written by this handler).
        reply_steam = steam_offers_host.handle_message(
            session, _csfloat_payload(input_source="steam", offers=[
                {
                    "wear_name": "Battle-Scarred", "float_value": 0.9, "price": 1.0,
                    "stattrak": False, "souvenir": False, "listing_id": "l99", "listing_type": "buy_now",
                }
            ])
        )
        row_steam = next(r for r in reply_steam["table"]["rows"] if r["wear_name"] == "Battle-Scarred")
        assert row_steam["input_cell"] == {"value": None, "color": None}


def _ten_csfloat_offers(start_price: float = 1.0, *, stattrak: bool = False, listing_type: str = "buy_now") -> list[dict]:
    return [
        {
            "wear_name": "Field-Tested",
            "float_value": 0.02 + i * 0.001,
            "pattern_seed": i,
            "price": start_price + i,
            "stattrak": stattrak,
            "souvenir": False,
            "listing_id": f"csfloat-{stattrak}-{i}",
            "listing_type": listing_type,
        }
        for i in range(10)
    ]


class TestHandleConstructContractCsfloat:
    def test_builds_contract_from_payload_offers_only_and_still_writes_to_disk(self, session):
        _make_skin(session, id="in-a", name="Input A")
        _make_skin(session, id="out-a", name="Output A", rarity_name="Restricted")
        signals.append_price_observations(
            "out-a",
            [signals.PriceObservationSignal(source="test", wear_name="Factory New", price=100.0, fetched_at=now_utc())],
        )
        # Stale disk data for the same skin that this handler must NOT draw
        # from -- if it leaked in, the cheapest 10 would include this $0.01
        # offer instead of matching _ten_csfloat_offers()'s real_cost exactly.
        signals.append_market_offers(
            "in-a",
            [
                signals.MarketOfferSignal(
                    source="csfloat", listing_id="stale", market_hash_name="Input A (Field-Tested)",
                    wear_name="Field-Tested", float_value=0.5, price=0.01, listing_type="buy_now",
                    fetched_at=now_utc(),
                )
            ],
        )

        reply = steam_offers_host.handle_message(
            session,
            _csfloat_payload(action="construct_contract_csfloat", offers=_ten_csfloat_offers()),
        )

        assert reply["ok"] is True
        assert reply["written"] == 10
        contract = reply["contract"]
        assert contract["skin_name"] == "Input A"
        assert contract["real_cost"] == pytest.approx(sum(1.0 + i for i in range(10)))
        assert len(contract["offers"]) == 10
        # pattern_seed round-trips even though MarketOfferSignal has no such
        # field -- it's recovered from `raw` (see _offer_pattern_seed).
        assert sorted(o["pattern_seed"] for o in contract["offers"]) == list(range(10))
        assert contract["outcomes"][0]["predicted_wear"] == "Factory New"

        on_disk = signals.read_market_offers("in-a")
        assert len(on_disk) == 11  # the 10 fresh + the pre-seeded stale one

        history = signals.read_contract_history("in-a")
        assert len(history) == 1
        assert reply["contract_history"] == [
            {
                "generated_at": history[0].generated_at.isoformat(),
                "expected_value": history[0].expected_value,
                "raw_avg_float": history[0].raw_avg_float,
            }
        ]

    def test_auction_listings_dont_count_toward_the_ten(self, session):
        _make_skin(session, id="in-a", name="Input A")

        offers = _ten_csfloat_offers()
        offers[0]["listing_type"] = "auction"
        reply = steam_offers_host.handle_message(
            session, _csfloat_payload(action="construct_contract_csfloat", offers=offers)
        )

        assert reply["ok"] is False
        assert "Only 9 buy-now listing" in reply["error"]
        # Still saved to disk, auction included -- just not usable as input cost.
        assert len(signals.read_market_offers("in-a")) == 10

    def test_best_combo_wins_across_stattrak_and_normal_groups(self, session):
        _make_skin(session, id="normal", name="Input A", stattrak=False)
        _make_skin(session, id="st", name="Input A", stattrak=True)
        _make_skin(session, id="out-normal", name="Output Normal", rarity_name="Restricted", stattrak=False)
        _make_skin(session, id="out-st", name="Output ST", rarity_name="Restricted", stattrak=True)
        signals.append_price_observations(
            "out-normal",
            [signals.PriceObservationSignal(source="test", wear_name="Factory New", price=5.0, fetched_at=now_utc())],
        )
        signals.append_price_observations(
            "out-st",
            [signals.PriceObservationSignal(source="test", wear_name="Factory New", price=500.0, fetched_at=now_utc())],
        )

        reply = steam_offers_host.handle_message(
            session,
            _csfloat_payload(
                action="construct_contract_csfloat",
                offers=_ten_csfloat_offers(stattrak=False) + _ten_csfloat_offers(stattrak=True, start_price=1.0),
            ),
        )

        assert reply["ok"] is True
        # The StatTrak group's output is priced far higher -- its combo must
        # be the one that wins the cross-group EV comparison.
        assert reply["contract"]["stattrak"] is True
        assert reply["skin_name"] == "Input A"

    def test_fewer_than_ten_buy_now_listings_returns_error(self, session):
        _make_skin(session, id="in-a", name="Input A")

        reply = steam_offers_host.handle_message(
            session,
            _csfloat_payload(action="construct_contract_csfloat", offers=_ten_csfloat_offers()[:9]),
        )

        assert reply["ok"] is False
        assert "Only 9 buy-now listing" in reply["error"]

    def test_no_resolvable_groups_returns_error(self, session):
        reply = steam_offers_host.handle_message(
            session, _csfloat_payload(action="construct_contract_csfloat")
        )

        assert reply["ok"] is False
        assert "No matching skin" in reply["error"]


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
