from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Skin(Base):
    """A tradeable weapon skin: one weapon pattern x StatTrak/Normal/Souvenir variant.

    Deliberately *not* split further by wear condition — a skin can be Factory New
    through Battle-Scarred, and wear-specific price detail lives in that per-wear
    granularity inside this skin's JSON signal files (see braindamage/signals.py),
    not as separate SQL rows. That keeps float-vs-price analysis possible from the
    raw signal data while SQL only carries one row (and one on-disk folder) per
    tradeable listing.

    `last_price`/`last_price_recalculated_at`/`last_price_calculation_data_point_recency`
    are calculated fields, refreshed from this skin's signals by
    braindamage.pricing.recalculate_last_price — never written by hand.
    """

    __tablename__ = "skins"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    weapon_name: Mapped[str | None] = mapped_column(String)
    pattern_name: Mapped[str | None] = mapped_column(String)
    category_name: Mapped[str | None] = mapped_column(String)
    rarity_name: Mapped[str | None] = mapped_column(String)
    rarity_color: Mapped[str | None] = mapped_column(String)
    min_float: Mapped[float | None] = mapped_column(Float)
    max_float: Mapped[float | None] = mapped_column(Float)
    stattrak: Mapped[bool] = mapped_column(Boolean, default=False)
    souvenir: Mapped[bool] = mapped_column(Boolean, default=False)
    # Doppler/Gamma Doppler disambiguator — market_hash_name alone collides across phases
    # (Ruby/Sapphire/Black Pearl/Emerald/Phase 1-4 share one Steam listing name).
    phase: Mapped[str | None] = mapped_column(String)
    paint_index: Mapped[str | None] = mapped_column(String)
    # Denormalized rather than a Collection table — Skin and Contract are the only
    # first-class entities; collection membership is static catalog data, not
    # something queried/joined across rows.
    collection_id: Mapped[str | None] = mapped_column(String)
    collection_name: Mapped[str | None] = mapped_column(String)
    image_url: Mapped[str | None] = mapped_column(String)

    last_price: Mapped[float | None] = mapped_column(Float)
    last_price_recalculated_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_price_calculation_data_point_recency: Mapped[datetime | None] = mapped_column(DateTime)


class Contract(Base):
    """A simulated trade-up contract, keyed by the content hash of its exact input
    composition (see braindamage.contracts.contract_id) — re-simulating the same 10
    inputs upserts this row rather than creating a duplicate.

    `input_lines` and `outcomes` are immutable snapshots produced by one
    deterministic simulation call and never filtered/sorted independently in SQL,
    so they're stored as JSON (mirroring how the old schema used a JSON `raw`
    column for per-row passthrough data). The scalar fields the contracts list page
    actually sorts/groups by (EV, ROI, CVaR, favorite, timestamps) are real columns.

    `ev_curve` is likewise a snapshot: samples of EV vs. hypothetical average
    input float (see braindamage.tradeup.simulate_ev_curve), computed once per
    simulation -- never recomputed ad hoc when the detail view renders -- so the
    chart there always reflects whatever prices this row was last simulated with.
    """

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    rarity_name: Mapped[str] = mapped_column(String, nullable=False)
    target_rarity_name: Mapped[str] = mapped_column(String, nullable=False)
    stattrak: Mapped[bool] = mapped_column(Boolean, nullable=False)

    input_cost: Mapped[float] = mapped_column(Float, nullable=False)
    expected_output_value: Mapped[float] = mapped_column(Float, nullable=False)
    expected_value: Mapped[float] = mapped_column(Float, nullable=False)
    roi: Mapped[float | None] = mapped_column(Float)
    cvar_5pct: Mapped[float | None] = mapped_column(Float)

    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_simulated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    input_lines: Mapped[list] = mapped_column(JSON, nullable=False)
    outcomes: Mapped[list] = mapped_column(JSON, nullable=False)
    missing_input_price_names: Mapped[list] = mapped_column(JSON, nullable=False)
    missing_output_price_names: Mapped[list] = mapped_column(JSON, nullable=False)
    ev_curve: Mapped[list] = mapped_column(JSON, nullable=False)
    ev_curve_annotations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    optimization_ranges: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
