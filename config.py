"""
Centralized configuration for PhD Finder.

All paths are resolved from environment variables with sensible defaults.
For local development: defaults assume the scipeds cache at ~/Library/Caches/.scipeds/
For deployment: set env vars to point at your bundled data files.
"""

import json
from datetime import datetime
from pathlib import Path

import os

# ── Base directories ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
NSF_CACHE_DIR = DATA_DIR / "nsf_cache"

# ── IPEDS DuckDB path ────────────────────────────────────────────────────
# Resolution order:
#   1. IPEDS_DB_PATH env var (explicit override)
#   2. ./data/scipeds.duckdb (Docker build by build_cache.py)
#   3. ./data/ipeds.duckdb (refresh_cache.py local export)
#   4. ~/Library/Caches/.scipeds/scipeds_0_0_8.duckdb (local dev)
def _resolve_db_path() -> Path:
    env = os.environ.get("IPEDS_DB_PATH")
    if env:
        return Path(env)
    for name in ("scipeds.duckdb", "ipeds.duckdb"):
        candidate = DATA_DIR / name
        if candidate.exists():
            return candidate
    return Path.home() / "Library/Caches/.scipeds/scipeds_0_0_8.duckdb"

DB_PATH = _resolve_db_path()

# ── NSF award cache CSV (pre-built by refresh_cache.py) ──────────────────
NSF_CSV_PATH = DATA_DIR / "nsf_awards_cache.csv"

# ── PhD verification CSV (pre-built by verify_cs_phd.py) ─────────────────
VERIFICATION_CSV = DATA_DIR / "cs_phd_verification.csv"
# Also check project root for backwards compat
if not VERIFICATION_CSV.exists():
    VERIFICATION_CSV = PROJECT_ROOT / "cs_phd_verification.csv"

# ── Metadata ──────────────────────────────────────────────────────────────
METADATA_PATH = DATA_DIR / "metadata.json"


def get_metadata() -> dict:
    """Load metadata (last refreshed timestamp, record counts, etc.)."""
    if METADATA_PATH.exists():
        with open(METADATA_PATH) as f:
            return json.load(f)
    return {}


def write_metadata(info: dict):
    """Write metadata dict to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = get_metadata()
    existing.update(info)
    existing["updated_at"] = datetime.now().isoformat()
    with open(METADATA_PATH, "w") as f:
        json.dump(existing, f, indent=2)


# ── Input validation ──────────────────────────────────────────────────────
MAX_QUERY_LENGTH = 200
MAX_KEYWORDS = 10


def sanitize_input(text: str) -> str:
    """Strip and reject suspicious characters from user input.
    Allows alphanumeric, spaces, hyphens, ampersands, apostrophes, periods, and commas.
    """
    text = text.strip()[:MAX_QUERY_LENGTH]
    # Remove anything that isn't a normal character
    return re.sub(r"[^a-zA-Z0-9\s\-&',.\(\)/]", "", text)


import re  # placed at end to avoid circular; used only in sanitize_input
