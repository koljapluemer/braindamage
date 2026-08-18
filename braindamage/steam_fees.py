"""Exact Steam Community Market seller-fee math.

Ports Steam's own client-side fee functions -- CalculateFeeAmount and
CalculateAmountToSendForDesiredReceivedAmount, from economy_common.js, the
JS Steam ships on every Market page (public, unminified, not proprietary
server code) -- instead of approximating with a flat percentage.

The two cuts (a 5% Steam fee, plus a 10% "game fee" for CS2/CS:GO, Dota 2,
TF2) are NOT taken off the gross buyer-pays price. They're computed ON TOP
of the net you-receive amount and added to it to produce buyer-pays:

    buyer_pays = you_receive
               + max(floor(you_receive * 0.05), $0.01)   # Steam fee
               + max(floor(you_receive * 0.10), $0.01)   # game fee

each floored independently, each with its own $0.01 minimum. So the
correct *approximate* net fraction of a gross price is 1/1.15 (~86.96%),
not 0.85 (~85%) -- treating the 15% as a cut off the gross price
(`gross * 0.85`, what this codebase did before) systematically
understates net proceeds by ~2 percentage points, before even accounting
for the cent-level floor rounding below.

Every price this app observes off Steam Market (a sell listing's ask, or a
buy-order book's "N requests to buy at $X or lower") IS that gross
buyer-pays figure -- so going net_proceeds() always means gross -> net,
which has no closed form (two independent floors aren't invertible in one
step). Steam's own client code handles this by guessing a starting
`received` amount from the un-floored math, then walking that guess up or
down until CalculateAmountToSendForDesiredReceivedAmount of it reproduces
the observed gross price exactly (at most a few cents' worth of
iterations) -- this is a faithful port of that same walk, not a
re-derivation.

Only USD is modeled: this app assumes USD everywhere downstream (see
steam_offers_host's EUR->USD conversion at the point of entry), and USD
isn't one of the currencies Steam switched to round-to-nearest (rather
than floor) for in its December 2025 market fee changes, so floor is
correct here.
"""

from __future__ import annotations

STEAM_FEE_PERCENT = 0.05
CS_PUBLISHER_FEE_PERCENT = 0.10
MIN_FEE_CENTS = 1
_MAX_ITERATIONS = 10

# The two cuts converge to this fraction of the GROSS price for any listing
# big enough that the $0.01-per-fee minimums and cent rounding are
# negligible (1 - 1/1.15 = ~13.04%) -- display-only: describes the typical
# effective cut for a UI label/tooltip ("about 13%"), never used in the
# actual net_proceeds math above, which is always cent-exact.
NOMINAL_CUT_OF_GROSS = 1 - 1 / (1 + STEAM_FEE_PERCENT + CS_PUBLISHER_FEE_PERCENT)


def _amount_to_send_cents(received_cents: int, publisher_fee_percent: float) -> tuple[int, int, int]:
    """Port of CalculateAmountToSendForDesiredReceivedAmount: the exact
    (steam_fee, publisher_fee, total buyer-pays) for one candidate net
    `received_cents`, all in integer cents. `received_cents` may go
    negative mid-walk in net_proceeds_cents below; floor-ing a negative
    product would misbehave with plain `int()` truncation, so this clamps
    the fee inputs at 0 rather than replicate that undefined territory --
    Steam's own UI never lets a listing price go this low anyway."""
    received_cents = max(received_cents, 0)
    steam_fee = max(int(received_cents * STEAM_FEE_PERCENT), MIN_FEE_CENTS)
    publisher_fee = (
        max(int(received_cents * publisher_fee_percent), MIN_FEE_CENTS) if publisher_fee_percent > 0 else 0
    )
    return steam_fee, publisher_fee, received_cents + steam_fee + publisher_fee


def net_proceeds_cents(gross_cents: int, *, publisher_fee_percent: float = CS_PUBLISHER_FEE_PERCENT) -> int:
    """Port of CalculateFeeAmount: given the gross buyer-pays amount (an
    observed Steam Market listing ask or buy-order price, in integer
    cents), returns the net cents a seller fulfilling it would actually
    receive after Steam's fee split."""
    if gross_cents <= 0:
        return 0

    estimate = int(gross_cents / (STEAM_FEE_PERCENT + publisher_fee_percent + 1))
    steam_fee, publisher_fee, total = _amount_to_send_cents(estimate, publisher_fee_percent)
    ever_undershot = False
    iterations = 0
    while total != gross_cents and iterations < _MAX_ITERATIONS:
        if total > gross_cents:
            if ever_undershot:
                # Both directions have been tried and neither guess lands
                # exactly -- Steam's own code stops walking here and just
                # absorbs the remaining cent(s) of difference into the
                # Steam fee, rather than continue oscillating.
                steam_fee, publisher_fee, total = _amount_to_send_cents(estimate - 1, publisher_fee_percent)
                steam_fee += gross_cents - total
                total = gross_cents
                break
            estimate -= 1
        else:
            ever_undershot = True
            estimate += 1
        steam_fee, publisher_fee, total = _amount_to_send_cents(estimate, publisher_fee_percent)
        iterations += 1

    return gross_cents - steam_fee - publisher_fee


def net_proceeds(gross_price: float, *, publisher_fee_percent: float = CS_PUBLISHER_FEE_PERCENT) -> float:
    """Dollar-in, dollar-out wrapper around net_proceeds_cents. `gross_price`
    must be the buyer-pays/gross figure -- exactly what every Steam Market
    price signal this app records (a listing ask, a buy-order summary
    price, or a last-sale price) actually is."""
    gross_cents = round(gross_price * 100)
    return net_proceeds_cents(gross_cents, publisher_fee_percent=publisher_fee_percent) / 100.0
