"""Latest known price per MarketItem, merged across the three price sources.

Mirrors the reduction pattern in pages/2_Skins.py's "last known price" query
(rank each source by row_number() over (partition by ... order by timestamp
desc), take rn == 1, merge by latest timestamp) — but scoped to MarketItem
(wear/StatTrak-specific) instead of Skin, since trade-up math needs the exact
variant's price, not a skin-level blended one. Callers here pass an explicit
market_item_id list and get it filtered *before* windowing, since they already
know exactly which items they need — unlike that page, which windows across the
whole catalog and can't filter first.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import HourlyListingPrice, HourlyMarketAggregate, PriceObservation


def latest_prices(session: Session, market_item_ids: list[str]) -> dict[str, float]:
    """Latest known price (any source; both hourly datasets and CS2Cap/CSV-sourced
    observations are USD, per docs/hourly-price-schema.md) per MarketItem.id in
    `market_item_ids`. An id missing from the result has no price data at all."""
    if not market_item_ids:
        return {}

    latest_by_item: dict[str, tuple[float, datetime]] = {}

    latest_po_ts = func.coalesce(PriceObservation.observed_at, PriceObservation.fetched_at)
    po_ranked = (
        select(
            PriceObservation.market_item_id.label("market_item_id"),
            PriceObservation.price.label("price"),
            latest_po_ts.label("ts"),
            func.row_number()
            .over(partition_by=PriceObservation.market_item_id, order_by=latest_po_ts.desc())
            .label("rn"),
        )
        .where(PriceObservation.market_item_id.in_(market_item_ids))
        .subquery()
    )
    po_rows = session.execute(
        select(po_ranked.c.market_item_id, po_ranked.c.price, po_ranked.c.ts).where(po_ranked.c.rn == 1)
    ).all()
    for row in po_rows:
        latest_by_item[row.market_item_id] = (row.price, row.ts)

    def _latest_from_hourly(value_col, ts_col, table):
        ranked = (
            select(
                table.market_item_id.label("market_item_id"),
                value_col.label("price"),
                ts_col.label("ts"),
                func.row_number()
                .over(partition_by=table.market_item_id, order_by=ts_col.desc())
                .label("rn"),
            )
            .where(table.market_item_id.in_(market_item_ids))
            .where(value_col.isnot(None))
            .subquery()
        )
        return session.execute(
            select(ranked.c.market_item_id, ranked.c.price, ranked.c.ts).where(ranked.c.rn == 1)
        ).all()

    for rows in (
        _latest_from_hourly(HourlyListingPrice.close_ask, HourlyListingPrice.bucket, HourlyListingPrice),
        _latest_from_hourly(HourlyMarketAggregate.ask, HourlyMarketAggregate.bucket, HourlyMarketAggregate),
    ):
        for row in rows:
            existing = latest_by_item.get(row.market_item_id)
            if existing is None or row.ts > existing[1]:
                latest_by_item[row.market_item_id] = (row.price, row.ts)

    return {item_id: price for item_id, (price, _ts) in latest_by_item.items()}
