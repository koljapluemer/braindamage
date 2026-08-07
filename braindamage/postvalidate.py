"""Postvalidates shortlisted mono trade-up contracts against CSFloat's live
floated listings -- confirms (or refutes) the wear-tier price approximation
the rest of this app runs on.

For each contract's buying-float ranges (braindamage.contracts'
_optimization_ranges, evaluated fresh via braindamage.report.evaluate_ranges):
checks whether 10 real listings actually exist within that exact float band
right now and what buying them would really cost (braindamage.csfloat_api,
recorded as MarketOfferSignal -- see braindamage.signals), and refreshes every
possible output's sell price from CSFloat's live lowest ask (recorded as an
ordinary PriceObservationSignal, so it flows through the existing
braindamage.pricing machinery like any other price source).

Only the genuinely ephemeral per-range facts (how many real listings existed,
what they cost) are persisted on the Contract row (models.Contract.
postvalidated_ranges) -- expected value is never cached, always rederived
fresh at render/filter time (see braindamage.report._repriced_with_real_cost),
consistent with how this app already treats every other EV number.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import contracts as contracts_module
from . import csfloat_api, pricing, report, signals
from .market_names import market_hash_name
from .models import Contract, Skin

# A mono trade-up always needs exactly 10 of its single input skin -- see
# tradeup.ContractState.is_ready / find_contracts.py (which only ever
# produces mono trades).
REQUIRED_INPUT_LISTINGS = 10


@dataclass
class RangePostvalidation:
    min_float: float
    max_float: float
    listings_found: int
    executable: bool
    real_input_cost: float | None
    checked_at: str


@dataclass
class ContractPostvalidationResult:
    contract_id: str
    ranges: list[RangePostvalidation] = field(default_factory=list)
    requests_made: int = 0
    # Set if a CSFloat call failed partway through (e.g. sustained rate
    # limiting) -- whatever ranges were already checked are still persisted,
    # same "keep partial progress, don't lose the whole run" convention as
    # cs2cap_api.PriceImportResult.error / steam_market_api.SteamPriceRefreshResult.error.
    error: str | None = None
    # Set when `error` came from csfloat_api.CsfloatMaxBackoffExceeded -- a
    # backoff that saturated at its ceiling usually means CSFloat is blocking
    # for hours, not seconds, so postvalidate_contracts treats this contract's
    # error as a reason to abort the whole batch rather than just move on.
    max_backoff_hit: bool = False


def _as_dict(rp: RangePostvalidation) -> dict:
    return {
        "min_float": rp.min_float,
        "max_float": rp.max_float,
        "listings_found": rp.listings_found,
        "executable": rp.executable,
        "real_input_cost": rp.real_input_cost,
        "checked_at": rp.checked_at,
    }


def postvalidate_contract(session: Session, contract: Contract) -> ContractPostvalidationResult:
    """Live-checks every buying-float range of `contract` against CSFloat and
    persists the result -- see module docstring. A contract with no range data
    (an incomplete/unpriced simulation) is left untouched and returns an empty
    result.

    CSFloat's rate limit is undocumented and *will* be hit on a large
    shortlist (see csfloat_api's adaptive backoff) -- if a call still fails
    after exhausting retries, this stops checking further ranges for this
    contract but still persists whatever ranges were already checked,
    exactly like cs2cap_api.run_price_import/steam_market_api.refresh_contract_prices
    do for their own price-fetch failures. The caller (postvalidate_contracts)
    moves on to the next contract rather than aborting the whole batch.
    """
    range_evals = report.evaluate_ranges(contract, session)
    if not range_evals:
        return ContractPostvalidationResult(contract.id)

    requests_made = 0
    # The outcome skin set is fixed per contract -- only which wear bucket
    # each lands in shifts between ranges -- so a (skin_id, wear) lookup is
    # cached across this contract's ranges to avoid redundant CSFloat calls.
    fetched_outputs: set[tuple[str, str]] = set()
    postvalidated: list[RangePostvalidation] = []
    error: str | None = None
    max_backoff_hit = False

    for r, detail in range_evals:
        try:
            inp = detail.inputs[0]
            input_skin = session.get(Skin, inp.skin_id)
            name = market_hash_name(input_skin, inp.wear_name)

            listings = csfloat_api.cheapest_listings_in_float_range(
                name, r["min_float"], r["max_float"], limit=REQUIRED_INPUT_LISTINGS
            )
            requests_made += 1
            if listings:
                signals.append_market_offers(
                    input_skin.id,
                    [
                        signals.MarketOfferSignal(
                            source="csfloat",
                            listing_id=listing.listing_id,
                            market_hash_name=listing.market_hash_name,
                            wear_name=listing.wear_name,
                            float_value=listing.float_value,
                            price=listing.price,
                            listing_type=listing.listing_type,
                            fetched_at=signals.now_utc(),
                            raw=listing.raw,
                        )
                        for listing in listings
                    ],
                )

            for o in detail.outcomes:
                key = (o.skin_id, o.predicted_wear)
                if key in fetched_outputs:
                    continue
                fetched_outputs.add(key)
                output_skin = session.get(Skin, o.skin_id)
                ask = csfloat_api.lowest_ask(market_hash_name(output_skin, o.predicted_wear))
                requests_made += 1
                if ask is not None:
                    signals.append_price_observations(
                        output_skin.id,
                        [
                            signals.PriceObservationSignal(
                                source="csfloat", wear_name=o.predicted_wear, price=ask,
                                fetched_at=signals.now_utc(),
                            )
                        ],
                    )
                    pricing.recalculate_last_price(output_skin)

            postvalidated.append(
                RangePostvalidation(
                    min_float=r["min_float"],
                    max_float=r["max_float"],
                    listings_found=len(listings),
                    executable=len(listings) >= REQUIRED_INPUT_LISTINGS,
                    real_input_cost=sum(listing.price for listing in listings) if listings else None,
                    checked_at=signals.now_utc().isoformat(),
                )
            )
        except csfloat_api.CsfloatAPIError as exc:
            error = str(exc)
            max_backoff_hit = isinstance(exc, csfloat_api.CsfloatMaxBackoffExceeded)
            break

    # Refreshes the contract's own approximate ev_curve/optimization_ranges
    # from whatever CSFloat output prices were written before any failure --
    # same closing step cs2cap_api.refresh_contract_prices/
    # steam_market_api.refresh_contract_prices use after any price-signal update.
    contracts_module.resimulate(session, contract)

    contract.postvalidated_ranges = [_as_dict(rp) for rp in postvalidated]
    contract.postvalidated_at = signals.now_utc()
    session.commit()

    return ContractPostvalidationResult(contract.id, postvalidated, requests_made, error, max_backoff_hit)


def postvalidate_contracts(
    session: Session,
    contracts: list[Contract],
    *,
    on_progress: Callable[[int, int], None] | None = None,
    max_consecutive_errors: int = 3,
    max_total_seconds: float = 1200.0,
) -> list[ContractPostvalidationResult]:
    """Runs postvalidate_contract for every contract in `contracts`
    (typically report.Selection.contracts, the already-shortlisted set --
    postvalidation is deliberately not run against every simulated contract,
    only the promising ones a report would actually display).
    `on_progress(done, total)`, if given, is called once per contract.

    One contract failing (postvalidate_contract already isolates a mid-run
    CSFloat failure to itself, but this is a second line of defense against
    anything unexpected) never stops the rest of the batch by itself -- losing
    an entire shortlist's worth of already-completed work over one contract's
    error is exactly the failure mode this whole function exists to avoid.

    Three circuit breakers stop the *whole* batch early, on top of that,
    since "never give up on one contract" and "never let the run be
    effectively unbounded" are both real requirements: `max_consecutive_errors`
    (CSFloat is sustained-unhappy, not just one flaky contract -- retrying the
    next 30 contracts at the same degraded odds just multiplies the wasted
    wall-clock), `max_total_seconds` (a hard ceiling regardless of cause), and
    a CsfloatMaxBackoffExceeded on any single contract (its backoff already
    saturated at csfloat_api._MAX_BACKOFF_SECONDS -- that usually means we're
    blocked for hours, so there's no reason to keep spending the remaining
    contracts' worth of wall-clock finding that out again on each one).
    Contracts left unattempted when any of these trips get no
    ContractPostvalidationResult at all -- they're simply not in the returned
    list, same as if postvalidation had never been requested for them.
    """
    results = []
    total = len(contracts)
    consecutive_errors = 0
    started_at = time.monotonic()

    for done, contract in enumerate(contracts, start=1):
        elapsed = time.monotonic() - started_at
        if elapsed > max_total_seconds:
            print(
                f"Postvalidation stopped after {elapsed:.0f}s (limit {max_total_seconds:.0f}s) -- "
                f"{done - 1}/{total} contract(s) attempted, keeping results gathered so far.",
                file=sys.stderr,
            )
            break

        try:
            result = postvalidate_contract(session, contract)
        except csfloat_api.CsfloatAPIError as exc:
            session.rollback()
            result = ContractPostvalidationResult(
                contract.id, error=str(exc), max_backoff_hit=isinstance(exc, csfloat_api.CsfloatMaxBackoffExceeded)
            )
        results.append(result)

        consecutive_errors = consecutive_errors + 1 if result.error is not None else 0
        if on_progress is not None:
            on_progress(done, total)
        if result.max_backoff_hit:
            print(
                f"Postvalidation stopped: CSFloat's backoff hit its {csfloat_api._MAX_BACKOFF_SECONDS:.0f}s "
                f"ceiling on contract {done}/{total} -- that usually means we're blocked for hours, so the "
                f"remaining {total - done} contract(s) were skipped instead of repeating the same wait on "
                "each one.",
                file=sys.stderr,
            )
            break
        if consecutive_errors >= max_consecutive_errors:
            print(
                f"Postvalidation stopped: {consecutive_errors} contracts in a row hit an error -- "
                "CSFloat looks sustained-unhappy rather than one flaky contract, so the remaining "
                f"{total - done} contract(s) were skipped instead of retried at the same degraded odds.",
                file=sys.stderr,
            )
            break

    return results
