"""Surveys every collection x rarity-tier ("collectionXtier") group that's usable as a
trade-up input (braindamage.tradeup.eligible_input_skins -- already excludes dead-end
groups whose collection has no valid output rarity) and prices every candidate normal
(non-StatTrak) skin in it via SteamApis' CSFloat marketplace data
(braindamage.steamapis_api), keeping the cheapest few per group: potential good buys for
a mono trade-up.

Distinct from braindamage.mono_trades: that module builds/simulates full 10x Contract
rows from the single cheapest input per group. This module is a pure price survey -- no
contracts are built -- and deliberately surfaces more than one option per group, since
"what's worth buying right now" benefits from seeing alternatives, not just one number.

Fetched prices are written through the same channels every other price source in this
app uses: braindamage.signals.PriceObservationSignal (typed-JSON-to-disk, append-only)
plus Skin.last_price via braindamage.pricing.recalculate_last_price -- so a buy
candidate's price shown here is the exact number the rest of the app (contract
simulation, the Qt UI) would already use for that skin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import pricing, signals, steamapis_api, tradeup
from .market_names import market_hash_name
from .models import Skin

DEFAULT_TOP_N_PER_GROUP = 3


@dataclass
class SkinCandidate:
    skin: Skin
    wear_name: str
    price: float


@dataclass
class GroupCandidates:
    collection_id: str
    collection_name: str
    rarity_name: str
    # Cheapest-first, already capped to top_n_per_group.
    candidates: list[SkinCandidate]


@dataclass
class SurveyResult:
    groups: list[GroupCandidates] = field(default_factory=list)
    requests_made: int = 0
    skins_priced: int = 0
    # Set if a SteamApis call failed partway through (rate limit, connection error, bad
    # response, ...) -- whatever was fetched and priced before that point is still kept
    # (both the signal files already written to disk and skin.last_price recalculated
    # in `session`, committed by the caller), same "keep partial progress, don't lose
    # the whole run" convention as cs2cap_api.PriceImportResult.error.
    error: str | None = None


def _eligible_groups(session: Session) -> list[tuple[str, str, str, list[Skin]]]:
    """(collection_id, collection_name, rarity_name, skins) for every collectionXtier
    that's a legal (non-dead-end) trade-up input group, normal (non-StatTrak) skins
    only -- StatTrak is deliberately excluded per the brief (buy candidates are for
    building a mono trade-up cheaply, and StatTrak inputs are consistently pricier for
    the same rarity/collection)."""
    groups: dict[tuple[str, str], list[Skin]] = {}
    collection_names: dict[str, str] = {}
    for rarity_name in tradeup.INPUT_RARITIES:
        for skin in tradeup.eligible_input_skins(session, rarity_name, stattrak=False):
            key = (skin.collection_id, rarity_name)
            groups.setdefault(key, []).append(skin)
            collection_names[skin.collection_id] = skin.collection_name

    return [
        (collection_id, collection_names[collection_id], rarity_name, skins)
        for (collection_id, rarity_name), skins in groups.items()
    ]


def _cheapest_csfloat_price(skin: Skin) -> tuple[str, float] | None:
    """Fetches this skin's SteamApis/CSFloat price at every standard wear bucket and
    returns the cheapest (wear_name, price) found, writing each successful reading to
    disk as a PriceObservationSignal along the way. Raises SteamApisAPIError if any
    call fails -- whatever wears were already fetched before that are already
    persisted to disk (append-only), so nothing already found is lost."""
    best: tuple[str, float] | None = None
    for wear_name, _lo, _hi in tradeup.WEAR_BUCKETS:
        name = market_hash_name(skin, wear_name)
        quote = steamapis_api.fetch_csfloat_price(name)
        if quote is None:
            continue
        signals.append_price_observations(
            skin.id,
            [
                signals.PriceObservationSignal(
                    source="steamapis_csfloat",
                    wear_name=wear_name,
                    price=quote.price,
                    fetched_at=signals.now_utc(),
                    raw=quote.raw,
                )
            ],
        )
        if best is None or quote.price < best[1]:
            best = (wear_name, quote.price)
    return best


def survey_cheapest_tradeup_buys(
    session: Session,
    *,
    top_n_per_group: int = DEFAULT_TOP_N_PER_GROUP,
    on_progress: Callable[[int, int], None] | None = None,
) -> SurveyResult:
    """For every collectionXtier trade-up input group, fetches fresh CSFloat prices (via
    SteamApis) for every candidate normal skin and keeps the `top_n_per_group` cheapest,
    ranked by their cheapest wear. `on_progress(done, total)`, if given, is called once
    per skin priced (across every group -- the natural unit of work here, one SteamApis
    request per wear per skin).

    Stops fetching (but keeps everything found so far, and still commits it) the moment
    a SteamApis call fails -- see SurveyResult.error and cs2cap_api.run_price_import for
    the same convention elsewhere in this app.
    """
    groups = _eligible_groups(session)
    total_skins = sum(len(skins) for *_rest, skins in groups)
    done = 0
    requests_made = 0
    skins_priced = 0
    error: str | None = None
    result_groups: list[GroupCandidates] = []

    for collection_id, collection_name, rarity_name, skins in groups:
        if error is not None:
            break
        priced: list[SkinCandidate] = []
        for skin in skins:
            if error is not None:
                break
            try:
                best = _cheapest_csfloat_price(skin)
            except steamapis_api.SteamApisAPIError as exc:
                error = str(exc)
                break
            finally:
                # Every wear bucket is attempted regardless of how many quotes came
                # back, so this counts as one logical unit of API usage per skin for
                # progress purposes -- the exact request count is tracked separately.
                done += 1
                if on_progress is not None:
                    on_progress(done, total_skins)
            requests_made += len(tradeup.WEAR_BUCKETS)
            if best is not None:
                pricing.recalculate_last_price(skin)
                skins_priced += 1
                priced.append(SkinCandidate(skin=skin, wear_name=best[0], price=best[1]))

        priced.sort(key=lambda c: c.price)
        if priced:
            result_groups.append(
                GroupCandidates(
                    collection_id=collection_id,
                    collection_name=collection_name,
                    rarity_name=rarity_name,
                    candidates=priced[:top_n_per_group],
                )
            )

    # One commit at the end, regardless of where an error broke the loop above --
    # every signal file write already happened (append-only, immediately durable);
    # this just persists the Skin.last_price recalculations gathered so far.
    session.commit()

    return SurveyResult(
        groups=result_groups, requests_made=requests_made, skins_priced=skins_priced, error=error
    )
