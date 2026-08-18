import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CS2CAP_API_KEY = os.environ.get("CS2CAP_API_KEY")

# Set once a Starter+ subscription is active -- unlocks POST /prices/batch (up
# to 100 items/request) in place of one GET /prices call per wear bucket, and
# the Maintenance page's bulk "refetch all skin prices" action. The CS2Cap API
# itself has no way to report a key's tier (GET /account/key returns rate
# limit/quota numbers but no plan name), so this has to be set by hand.
CS2CAP_PREMIUM_TIER = os.environ.get("CS2CAP_PREMIUM_TIER", "").strip().lower() in ("1", "true", "yes")

# CSFloat (https://docs.csfloat.com) -- free with a CSFloat account, no tier
# gating. Used only for postvalidation (braindamage.postvalidate): CS2Cap
# never sees individual floated listings, only wear-tier aggregates.
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")

# SteamApis (https://docs.steamapis.com) -- paid, key from the SteamApis
# dashboard. Used by braindamage.steamapis_api/tradeup_buys to read CSFloat
# marketplace prices through SteamApis' Market Data REST API rather than
# CSFloat's own API (braindamage.csfloat_api), which is reserved for
# postvalidation's tighter, listing-level checks.
STEAMAPIS_KEY = os.environ.get("STEAMAPIS_KEY")

# Static EUR->USD rate for braindamage.steam_offers_host -- this app assumes
# USD everywhere (pricing, EV math), so a EUR-currency Steam Market scrape is
# converted to USD once, at write time, using this rate. Deliberately a
# hand-maintained number, not a live-fetched one: the native host otherwise
# makes no network calls at all, and FX doesn't move enough day-to-day to
# matter for evaluating a trade-up buy. Update it by hand every so often.
_EUR_USD_RATE_RAW = os.environ.get("EUR_USD_RATE")
EUR_USD_RATE = float(_EUR_USD_RATE_RAW) if _EUR_USD_RATE_RAW else None
