"""Steam market_hash_name construction -- shared by every price-fetching
client (braindamage.cs2cap_api, braindamage.steam_market_api) so they can
never drift apart on how a skin's name is built.
"""

from __future__ import annotations

from .models import Skin


def market_hash_name(skin: Skin, wear_name: str) -> str:
    if skin.stattrak:
        prefix = "StatTrak™ "
    elif skin.souvenir:
        prefix = "Souvenir "
    else:
        prefix = ""
    return f"{prefix}{skin.name} ({wear_name})"
