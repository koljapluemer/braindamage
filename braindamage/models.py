from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String)

    skins: Mapped[list["Skin"]] = relationship(back_populates="collection")


class Skin(Base):
    __tablename__ = "skins"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    weapon_name: Mapped[str | None] = mapped_column(String)
    category_name: Mapped[str | None] = mapped_column(String)
    pattern_name: Mapped[str | None] = mapped_column(String)
    rarity_name: Mapped[str | None] = mapped_column(String)
    rarity_color: Mapped[str | None] = mapped_column(String)
    min_float: Mapped[float | None] = mapped_column(Float)
    max_float: Mapped[float | None] = mapped_column(Float)
    stattrak: Mapped[bool] = mapped_column(Boolean, default=False)
    souvenir: Mapped[bool] = mapped_column(Boolean, default=False)
    has_normal_variant: Mapped[bool] = mapped_column(Boolean, default=True)
    paint_index: Mapped[str | None] = mapped_column(String)
    image_url: Mapped[str | None] = mapped_column(String)
    collection_id: Mapped[str | None] = mapped_column(ForeignKey("collections.id"))

    collection: Mapped["Collection | None"] = relationship(back_populates="skins")
    market_items: Mapped[list["MarketItem"]] = relationship(back_populates="skin")


class MarketItem(Base):
    """A tradeable variant of a Skin: one specific wear x StatTrak/Souvenir combination.

    Sourced from bymykel's skins_not_grouped.json — one row per row there. This is the
    identity that prices attach to, since Skin is a pattern-level catalog entry and isn't
    itself tradeable.
    """

    __tablename__ = "market_items"
    __table_args__ = (UniqueConstraint("market_hash_name", "phase"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    skin_id: Mapped[str] = mapped_column(ForeignKey("skins.id"), nullable=False)
    market_hash_name: Mapped[str] = mapped_column(String, nullable=False)
    wear_name: Mapped[str | None] = mapped_column(String)
    stattrak: Mapped[bool] = mapped_column(Boolean, default=False)
    souvenir: Mapped[bool] = mapped_column(Boolean, default=False)
    # Doppler/Gamma Doppler disambiguator — market_hash_name alone collides across phases
    # (Ruby/Sapphire/Black Pearl/Emerald/Phase 1-4 share one Steam listing name).
    phase: Mapped[str | None] = mapped_column(String)

    skin: Mapped["Skin"] = relationship(back_populates="market_items")
    price_observations: Mapped[list["PriceObservation"]] = relationship(back_populates="market_item")
    external_ids: Mapped[list["MarketItemExternalId"]] = relationship(back_populates="market_item")


class PriceObservation(Base):
    """One price reading for a MarketItem from a source, at a point in time.

    Append-only: rows are always inserted, never updated or deleted. This is what lets
    the app absorb new/changed price sources without losing history or requiring
    destructive migrations — anything not captured by the normalized columns below is
    preserved verbatim in `raw`.
    """

    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_item_id: Mapped[str] = mapped_column(ForeignKey("market_items.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str | None] = mapped_column(String)
    side: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    raw: Mapped[dict] = mapped_column(JSON, nullable=False)

    market_item: Mapped["MarketItem"] = relationship(back_populates="price_observations")


class HourlyListingPrice(Base):
    """Per-source hourly OHLC listing prices, from the cs2.sh historical export.

    One row per (market item x source x hour). Deliberately typed columns instead
    of PriceObservation's JSON `raw` blob: this table holds tens of millions of rows
    (one bulk import), so per-row JSON overhead would multiply the on-disk footprint
    many times over. Composite primary key doubles as the dedup key, so re-running an
    interrupted bulk import (via `INSERT OR IGNORE`) is safe.
    """

    __tablename__ = "hourly_listing_prices"

    market_item_id: Mapped[str] = mapped_column(ForeignKey("market_items.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    open_ask: Mapped[float | None] = mapped_column(Float)
    high_ask: Mapped[float | None] = mapped_column(Float)
    low_ask: Mapped[float | None] = mapped_column(Float)
    close_ask: Mapped[float | None] = mapped_column(Float)
    ask_volume: Mapped[int | None] = mapped_column(Integer)
    open_bid: Mapped[float | None] = mapped_column(Float)
    high_bid: Mapped[float | None] = mapped_column(Float)
    low_bid: Mapped[float | None] = mapped_column(Float)
    close_bid: Mapped[float | None] = mapped_column(Float)
    bid_volume: Mapped[int | None] = mapped_column(Integer)
    sample_count: Mapped[int | None] = mapped_column(Integer)

    market_item: Mapped["MarketItem"] = relationship()


class HourlyMarketAggregate(Base):
    """Cross-marketplace hourly aggregate, from the cs2.sh historical export.

    One row per (market item x hour). See HourlyListingPrice for why this uses typed
    columns rather than PriceObservation's JSON `raw` blob.
    """

    __tablename__ = "hourly_market_aggregates"

    market_item_id: Mapped[str] = mapped_column(ForeignKey("market_items.id"), primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    ask: Mapped[float | None] = mapped_column(Float)
    ask_volume: Mapped[int | None] = mapped_column(Integer)
    bid: Mapped[float | None] = mapped_column(Float)
    bid_volume: Mapped[int | None] = mapped_column(Integer)
    hourly_volume: Mapped[float | None] = mapped_column(Float)
    total_supply: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int | None] = mapped_column(Integer)

    market_item: Mapped["MarketItem"] = relationship()


class MarketItemExternalId(Base):
    """Crosswalk from a MarketItem to a source's own id for it.

    Lets a source's identifiers be cached without baking them into MarketItem itself —
    new sources just add rows here, nothing else changes.
    """

    __tablename__ = "market_item_external_ids"
    __table_args__ = (UniqueConstraint("market_item_id", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_item_id: Mapped[str] = mapped_column(ForeignKey("market_items.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)

    market_item: Mapped["MarketItem"] = relationship(back_populates="external_ids")
