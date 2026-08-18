from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from braindamage import signals, steam_fees
from braindamage.models import Base, Contract, Skin
from braindamage.mono_trades import _representative_float, generate_mono_trades
from braindamage.tradeup import INPUT_RARITIES


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


def _price(skin_id: str, wear_name: str, price: float) -> None:
    signals.append_price_observations(
        skin_id,
        [
            signals.PriceObservationSignal(
                source="cs2cap", wear_name=wear_name, price=price, fetched_at=datetime(2026, 1, 1),
            )
        ],
    )


class TestRepresentativeFloat:
    def test_bucket_midpoint_within_full_range_skin(self):
        skin = Skin(id="s", name="S", min_float=0.0, max_float=1.0)
        assert _representative_float(skin, "Field-Tested") == pytest.approx(0.265)

    def test_clamps_to_skin_range_when_bucket_partially_overlaps(self):
        skin = Skin(id="s", name="S", min_float=0.2, max_float=0.5)
        # Field-Tested is (0.15, 0.38); clipped to [0.2, 0.38] -> midpoint 0.29.
        assert _representative_float(skin, "Field-Tested") == pytest.approx(0.29)

    def test_falls_back_to_skin_midpoint_when_bucket_doesnt_overlap(self):
        skin = Skin(id="s", name="S", min_float=0.0, max_float=0.05)
        # Battle-Scarred is (0.45, 1.00) -- doesn't reach this skin's range at all.
        assert _representative_float(skin, "Battle-Scarred") == pytest.approx(0.025)


class TestGenerateMonoTrades:
    def _setup_two_collections(self, session):
        # Collection A: cheap input ($5/unit -> $50 for 10x), priced output.
        _make_skin(
            session, id="in-a", name="Cheap Input", rarity_name="Mil-Spec Grade",
            collection_id="col-a", collection_name="Collection A",
        )
        _make_skin(
            session, id="out-a", name="Output A", rarity_name="Restricted",
            collection_id="col-a", collection_name="Collection A",
        )
        _price("in-a", "Field-Tested", 5.0)
        _price("out-a", "Field-Tested", 200.0)

        # Collection B: expensive input ($50/unit -> $500 for 10x) -- over budget.
        _make_skin(
            session, id="in-b", name="Expensive Input", rarity_name="Mil-Spec Grade",
            collection_id="col-b", collection_name="Collection B",
        )
        _make_skin(
            session, id="out-b", name="Output B", rarity_name="Restricted",
            collection_id="col-b", collection_name="Collection B",
        )
        _price("in-b", "Field-Tested", 50.0)

    def test_filters_by_max_cost_and_ranks_by_ev(self, session):
        self._setup_two_collections(session)

        progress_calls: list[tuple[int, int]] = []
        rows = generate_mono_trades(
            session, max_input_cost=100.0, on_progress=lambda done, total: progress_calls.append((done, total))
        )

        assert [row.input_lines[0]["skin_id"] for row in rows] == ["in-a"]
        assert rows[0].input_cost == pytest.approx(50.0)
        # Net output (Steam's real fee on a $200 sale) minus 50 input cost.
        assert rows[0].expected_value == pytest.approx(steam_fees.net_proceeds(200.0) - 50.0)

        assert progress_calls == [(i, len(INPUT_RARITIES) * 2) for i in range(1, len(INPUT_RARITIES) * 2 + 1)]

        stored = list(session.scalars(select(Contract)))
        assert len(stored) == 1

    def test_rerunning_upserts_not_duplicates(self, session):
        self._setup_two_collections(session)

        generate_mono_trades(session, max_input_cost=100.0)
        generate_mono_trades(session, max_input_cost=100.0)

        assert len(list(session.scalars(select(Contract)))) == 1
