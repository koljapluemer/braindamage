"""Client for the bymykel/CSGO-API dataset (https://bymykel.com/CSGO-API/)."""

import json
import re
import urllib.request
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Collection, MarketItem, Skin

BASE_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en"

# Doppler/Gamma Doppler knives share one market_hash_name across phases (Ruby, Sapphire,
# Black Pearl, Emerald, Phase 1-4); bymykel doesn't label the phase directly, but it's
# recoverable from the pattern id (e.g. "am_doppler_phase2", "am_ruby_marbleized").
_NAMED_PHASE_PATTERNS = (
    ("ruby", "Ruby"),
    ("sapphire", "Sapphire"),
    ("blackpearl", "Black Pearl"),
    ("emerald", "Emerald"),
)


def _derive_phase(pattern_id: str | None) -> str | None:
    if not pattern_id:
        return None
    lowered = pattern_id.lower()
    for needle, phase in _NAMED_PHASE_PATTERNS:
        if needle in lowered:
            return phase
    match = re.search(r"phase(\d)", lowered)
    return f"Phase {match.group(1)}" if match else None


@dataclass
class ImportResult:
    collections: int
    skins: int
    market_items: int


def _fetch_json(name: str) -> list[dict]:
    with urllib.request.urlopen(f"{BASE_URL}/{name}") as response:
        return json.load(response)


def run_import() -> ImportResult:
    collections_data = _fetch_json("collections.json")
    skins_data = _fetch_json("skins.json")
    variants_data = _fetch_json("skins_not_grouped.json")

    normal_variant_ids = {
        v["skin_id"]
        for v in variants_data
        if not v.get("stattrak") and not v.get("souvenir")
    }

    with SessionLocal() as session:
        for data in collections_data:
            _upsert_collection(session, data)
        session.flush()

        for data in skins_data:
            _upsert_skin(session, data, has_normal_variant=data["id"] in normal_variant_ids)
        session.flush()

        for data in variants_data:
            _upsert_market_item(session, data)

        session.commit()

    return ImportResult(
        collections=len(collections_data),
        skins=len(skins_data),
        market_items=len(variants_data),
    )


def _upsert_collection(session: Session, data: dict) -> None:
    collection = session.get(Collection, data["id"])
    if collection is None:
        collection = Collection(id=data["id"])
        session.add(collection)

    collection.name = data["name"]
    collection.image_url = data.get("image")


def _upsert_skin(session: Session, data: dict, has_normal_variant: bool) -> None:
    skin = session.get(Skin, data["id"])
    if skin is None:
        skin = Skin(id=data["id"])
        session.add(skin)

    weapon = data.get("weapon") or {}
    category = data.get("category") or {}
    pattern = data.get("pattern") or {}
    rarity = data.get("rarity") or {}
    collections = data.get("collections") or []

    skin.name = data["name"]
    skin.description = data.get("description")
    skin.weapon_name = weapon.get("name")
    skin.category_name = category.get("name")
    skin.pattern_name = pattern.get("name")
    skin.rarity_name = rarity.get("name")
    skin.rarity_color = rarity.get("color")
    skin.min_float = data.get("min_float")
    skin.max_float = data.get("max_float")
    skin.stattrak = bool(data.get("stattrak"))
    skin.souvenir = bool(data.get("souvenir"))
    skin.paint_index = data.get("paint_index")
    skin.image_url = data.get("image")
    skin.collection_id = collections[0]["id"] if collections else None
    skin.has_normal_variant = has_normal_variant


def _upsert_market_item(session: Session, data: dict) -> None:
    market_item = session.get(MarketItem, data["id"])
    if market_item is None:
        market_item = MarketItem(id=data["id"])
        session.add(market_item)

    wear = data.get("wear") or {}
    pattern = data.get("pattern") or {}

    market_item.skin_id = data["skin_id"]
    market_item.market_hash_name = data["market_hash_name"]
    market_item.wear_name = wear.get("name")
    market_item.stattrak = bool(data.get("stattrak"))
    market_item.souvenir = bool(data.get("souvenir"))
    market_item.phase = _derive_phase(pattern.get("id"))
