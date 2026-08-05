"""One-off/rerunnable catalog import: populates Skin rows from bymykel's
CSGO-API dataset (https://bymykel.com/CSGO-API/).

Not part of the Textual app — catalog changes (new skins) are infrequent and
don't need a UI page, so this is just run by hand (`uv run python
scripts/import_catalog.py`) when the game gets an update. Groups
skins_not_grouped.json (wear-level) down to (pattern, StatTrak, Souvenir, phase)
— Skin no longer has a per-wear row, see braindamage/models.py — using
skins.json for the shared pattern-level metadata (name, weapon, rarity,
collection, float range).

Weapon skins only: knives and gloves are excluded, matching the trade-up
simulator's existing scope (braindamage.tradeup._NON_WEAPON_CATEGORIES).
"""

import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from braindamage.db import SessionLocal
from braindamage.models import Skin

BASE_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en"

_NON_WEAPON_CATEGORIES = {"Knives", "Gloves"}

# Doppler/Gamma Doppler knives share one market_hash_name across phases (Ruby,
# Sapphire, Black Pearl, Emerald, Phase 1-4); bymykel doesn't label the phase
# directly, but it's recoverable from the pattern id (e.g. "am_doppler_phase2").
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


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "skin"


def _skin_id(base_skin_id: str, stattrak: bool, souvenir: bool, phase: str | None) -> str:
    slug = _slugify(base_skin_id)
    if stattrak:
        slug += "-stattrak"
    if souvenir:
        slug += "-souvenir"
    if phase:
        slug += f"-{_slugify(phase)}"
    return slug


def _fetch_json(name: str) -> list[dict]:
    with urllib.request.urlopen(f"{BASE_URL}/{name}") as response:
        return json.load(response)


@dataclass
class ImportResult:
    base_skins: int
    variants_seen: int
    skins_written: int


def run_import() -> ImportResult:
    skins_data = _fetch_json("skins.json")
    variants_data = _fetch_json("skins_not_grouped.json")

    base_by_id = {s["id"]: s for s in skins_data}

    # Group wear-level variants down to (base_skin_id, stattrak, souvenir, phase).
    variant_groups: dict[tuple[str, bool, bool, str | None], dict] = {}
    for variant in variants_data:
        base = base_by_id.get(variant["skin_id"])
        if base is None:
            continue
        category = (base.get("category") or {}).get("name")
        if category in _NON_WEAPON_CATEGORIES:
            continue
        stattrak = bool(variant.get("stattrak"))
        souvenir = bool(variant.get("souvenir"))
        phase = _derive_phase((variant.get("pattern") or {}).get("id"))
        key = (variant["skin_id"], stattrak, souvenir, phase)
        variant_groups.setdefault(key, base)

    written = 0
    with SessionLocal() as session:
        for (base_skin_id, stattrak, souvenir, phase), base in variant_groups.items():
            skin_id = _skin_id(base_skin_id, stattrak, souvenir, phase)
            skin = session.get(Skin, skin_id)
            if skin is None:
                skin = Skin(id=skin_id)
                session.add(skin)

            weapon = base.get("weapon") or {}
            category = base.get("category") or {}
            pattern = base.get("pattern") or {}
            rarity = base.get("rarity") or {}
            collections = base.get("collections") or []
            collection = collections[0] if collections else None

            skin.name = base["name"]
            skin.weapon_name = weapon.get("name")
            skin.pattern_name = pattern.get("name")
            skin.category_name = category.get("name")
            skin.rarity_name = rarity.get("name")
            skin.rarity_color = rarity.get("color")
            skin.min_float = base.get("min_float")
            skin.max_float = base.get("max_float")
            skin.stattrak = stattrak
            skin.souvenir = souvenir
            skin.phase = phase
            skin.paint_index = base.get("paint_index")
            skin.collection_id = collection["id"] if collection else None
            skin.collection_name = collection["name"] if collection else None
            skin.image_url = base.get("image")
            written += 1

        session.commit()

    return ImportResult(
        base_skins=len(skins_data),
        variants_seen=len(variants_data),
        skins_written=written,
    )


if __name__ == "__main__":
    result = run_import()
    print(f"base skins (skins.json): {result.base_skins}")
    print(f"variants seen (skins_not_grouped.json): {result.variants_seen}")
    print(f"skins written: {result.skins_written}")
