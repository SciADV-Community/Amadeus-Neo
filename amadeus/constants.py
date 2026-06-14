import os
from pathlib import Path


# ============================================================
# Storage
# ============================================================

# Local default:
#   amadeus.sqlite3
#
# Docker recommended:
#   AMADEUS_DB_PATH=/app/data/amadeus.sqlite3
DB_PATH = Path(os.environ.get("AMADEUS_DB_PATH", "amadeus.sqlite3"))


# ============================================================
# Verification behavior
# ============================================================

MIN_ACCOUNT_AGE_DAYS = int(os.environ.get("AMADEUS_MIN_ACCOUNT_AGE_DAYS", "14"))
MAX_FAILED_ATTEMPTS = int(os.environ.get("AMADEUS_MAX_FAILED_ATTEMPTS", "3"))
CAPTCHA_LENGTH = int(os.environ.get("AMADEUS_CAPTCHA_LENGTH", "6"))
CAPTCHA_EXPIRY_MINUTES = int(os.environ.get("AMADEUS_CAPTCHA_EXPIRY_MINUTES", "10"))


# ============================================================
# Bot owner
# ============================================================

# Discord user ID of the bot owner.
# Used to gate /amadeus admin commands.
# If unset, /amadeus admin commands will always reject.
_owner_id_raw = os.environ.get("AMADEUS_OWNER_ID", "").strip()
OWNER_ID: int | None = int(_owner_id_raw) if _owner_id_raw else None


# ============================================================
# Application profile
# ============================================================

# Optional links appended to the Discord application's About Me / bio.
PRIVACY_POLICY_URL = os.environ.get("AMADEUS_PRIVACY_POLICY_URL", "").strip()
TERMS_OF_SERVICE_URL = os.environ.get("AMADEUS_TERMS_OF_SERVICE_URL", "").strip()


# ============================================================
# Backfill settings
# ============================================================

# This is intentionally slow because large servers can have 10,000+ users.
BACKFILL_DELAY_SECONDS = float(os.environ.get("AMADEUS_BACKFILL_DELAY_SECONDS", "1.5"))
VERIFICATION_ROLE_DELAY_SECONDS = float(os.environ.get("AMADEUS_VERIFICATION_ROLE_DELAY_SECONDS", "5"))

BACKFILL_INCLUDE_BOTS_BY_DEFAULT = (
    os.environ.get("AMADEUS_BACKFILL_INCLUDE_BOTS_BY_DEFAULT", "false").lower()
    in {"1", "true", "yes", "y", "on"}
)


# ============================================================
# Common error messages
# ============================================================

ERR_GUILD_ONLY = "This can only be used inside a server."
ERR_BOT_MEMBER_UNAVAILABLE = "Could not read my server member data."
