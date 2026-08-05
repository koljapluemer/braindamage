"""Shared skin dataset load + filter functions used by both the Maintenance
and Skins pages' SearchFilterBar instances -- the one place this near-
duplicate query+filter logic lives now, instead of copy-pasted per page.
"""

from __future__ import annotations

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Skin


def load_skins() -> list[Skin]:
    with SessionLocal() as session:
        return list(session.scalars(select(Skin).order_by(Skin.name)))


def filter_skins(skins: list[Skin], query: str) -> list[Skin]:
    needle = query.strip().lower()
    if not needle:
        return skins
    return [s for s in skins if needle in f"{s.name} {s.collection_name or ''}".lower()]
