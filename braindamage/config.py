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
