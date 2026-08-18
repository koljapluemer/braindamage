"""Steam market_hash_name construction -- shared by every price-fetching
client (braindamage.cs2cap_api, braindamage.steam_market_api) so they can
never drift apart on how a skin's name is built.
"""

from __future__ import annotations

from .models import Skin
from .tradeup import WEAR_BUCKETS

_WEAR_NAMES = [name for name, _lo, _hi in WEAR_BUCKETS]


def market_hash_name(skin: Skin, wear_name: str) -> str:
    if skin.stattrak:
        prefix = "StatTrak™ "
    elif skin.souvenir:
        prefix = "Souvenir "
    else:
        prefix = ""
    return f"{prefix}{skin.name} ({wear_name})"


def parse_market_hash_name(name: str) -> tuple[str, str | None, bool, bool]:
    """Reverses market_hash_name(): (base_name, wear_name, stattrak, souvenir).

    wear_name is None if no trailing " (<known wear>)" is found -- callers
    should treat that as a parse failure, not guess. Only strips a trailing
    parenthetical when it exactly matches one of the 5 canonical wear names,
    since some skin base names end in their own parenthetical (e.g.
    "M4A4 | 龍王 (Dragon King)") that must not be mistaken for a wear suffix.
    """
    wear_name = None
    for candidate in _WEAR_NAMES:
        suffix = f" ({candidate})"
        if name.endswith(suffix):
            wear_name = candidate
            name = name[: -len(suffix)]
            break

    if name.startswith("StatTrak™ "):
        return name[len("StatTrak™ ") :], wear_name, True, False
    if name.startswith("Souvenir "):
        return name[len("Souvenir ") :], wear_name, False, True
    return name, wear_name, False, False
