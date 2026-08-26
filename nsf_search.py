#!/usr/bin/env python3
"""
NSF Award Search module for the cs_phd_check project.

Queries the NSF Award Search API (no key required), caches raw JSON responses,
and returns a deduplicated DataFrame of awards matching given keywords.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from rapidfuzz import fuzz, process

# --- Configuration ---
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "nsf_cache"
CS_PHD_CSV = BASE_DIR / "cs_phd_verification.csv"

NSF_API_URL = "https://www.research.gov/awardapi-service/v1/awards.json"
NSF_PRINT_FIELDS = (
    "id,title,piFirstName,piLastName,coPDPI,awardeeName,"
    "awardeeStateCode,startDate,expDate,fundsObligatedAmt,abstractText"
)
REQUEST_DELAY = 1.0  # seconds between API calls (rate-limit politely)
CACHE_EXPIRY_DAYS = 7

MATCH_THRESHOLD = 85  # Same as verify_cs_phd.py


# --- Cache helpers ---

def _cache_key(keyword: str) -> str:
    """Generate a filesystem-safe cache key from a keyword."""
    today = datetime.now().strftime("%Y-%m-%d")
    raw = f"{keyword.lower().strip()}_{today}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_path(keyword: str) -> Path:
    """Return the cache file path for a keyword."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{_cache_key(keyword)}.json"


def _cache_is_fresh(path: Path) -> bool:
    """Check if a cache file exists and is within the expiry window."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(days=CACHE_EXPIRY_DAYS)


# --- API query ---

def _query_nsf_api(keyword: str) -> list[dict]:
    """Query the NSF Award Search API for a single keyword.
    Returns the raw award list from the API response.
    Uses local cache if available and fresh.
    """
    cache_file = _cache_path(keyword)
    if _cache_is_fresh(cache_file):
        with open(cache_file) as f:
            data = json.load(f)
        return data.get("response", {}).get("award", [])

    params = {
        "keyword": keyword,
        "printFields": NSF_PRINT_FIELDS,
        "offset": 0,
        "rpp": 250,
    }
    resp = requests.get(NSF_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Cache the full response
    with open(cache_file, "w") as f:
        json.dump(data, f)

    awards = data.get("response", {}).get("award", [])

    # Handle pagination if needed (NSF caps at ~10000 total)
    metadata = data.get("response", {}).get("metadata", {})
    total_count = metadata.get("totalCount", 0)
    rpp = metadata.get("rpp", 250)
    offset = metadata.get("offset", 0) + rpp

    while offset < total_count and offset < 10000:
        time.sleep(REQUEST_DELAY)
        params["offset"] = offset
        resp = requests.get(NSF_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        page_awards = page.get("response", {}).get("award", [])
        if not page_awards:
            break
        awards.extend(page_awards)
        # Cache the accumulated results
        data["response"]["award"] = awards
        with open(cache_file, "w") as f:
            json.dump(data, f)
        offset += rpp

    return awards


# --- Parsing ---

def _parse_awards(raw_awards: list[dict], keyword_matched: str) -> list[dict]:
    """Parse raw API awards into flat dicts with normalized fields."""
    parsed = []
    for award in raw_awards:
        # Parse PI name
        first = award.get("piFirstName", "") or ""
        last = award.get("piLastName", "") or ""
        pi_name = f"{first} {last}".strip()

        # Parse co-PIs
        co_pdpi = award.get("coPDPI") or []
        if isinstance(co_pdpi, str):
            co_pdpi = [co_pdpi]
        co_pi_names = []
        for entry in co_pdpi:
            # Format: "Name email@domain" — extract just the name part
            name_part = re.split(r"\s+\S+@\S+", entry)[0].strip()
            if name_part:
                co_pi_names.append(name_part)

        # Parse dates (MM/DD/YYYY format)
        start_str = award.get("startDate", "") or ""
        exp_str = award.get("expDate", "") or ""
        start_date = _parse_nsfd(start_str)
        exp_date = _parse_nsfd(exp_str)

        # Determine if active (expiration date is today or later)
        is_active = exp_date is not None and exp_date >= datetime.now().date()

        # Parse funds
        funds_str = award.get("fundsObligatedAmt", "") or "0"
        try:
            funds = float(re.sub(r"[^\d.]", "", funds_str))
        except (ValueError, TypeError):
            funds = 0.0

        # Abstract snippet (first ~300 chars, cleaned)
        abstract = award.get("abstractText", "") or ""
        abstract = re.sub(r"\r\n", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()
        snippet = abstract[:300] + ("..." if len(abstract) > 300 else "")

        parsed.append({
            "keyword_matched": keyword_matched,
            "award_id": award.get("id", ""),
            "title": (award.get("title", "") or "").strip(),
            "pi_name": pi_name,
            "co_pi_names": "; ".join(co_pi_names),
            "institution": (award.get("awardeeName", "") or "").strip(),
            "state": (award.get("awardeeStateCode", "") or "").strip(),
            "start_date": start_date,
            "exp_date": exp_date,
            "is_active": is_active,
            "funds_obligated": funds,
            "abstract_snippet": snippet,
        })
    return parsed


def _parse_nsfd(date_str: str):
    """Parse MM/DD/YYYY date string to date object. Returns None on failure."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").date()
    except ValueError:
        return None


# --- Cross-reference against CS PhD verification ---

def _load_phd_verification() -> pd.DataFrame:
    """Load the CS PhD verification CSV."""
    if not CS_PHD_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(CS_PHD_CSV)
    return df


def _match_institution_to_phd(institution: str, phd_lookup: dict,
                               threshold: int = MATCH_THRESHOLD) -> str:
    """Fuzzy-match an NSF institution name against the verified CS PhD data.
    Returns: 'True' if matched institution has CS PhD, 'False' if no PhD,
             'NotChecked' if no match found.
    """
    if not phd_lookup:
        return "NotChecked"

    # Normalize
    norm_inst = re.sub(r"[^a-z0-9 ]", "", institution.lower())

    # Build candidate list from phd_lookup keys
    candidates = {}
    for key, val in phd_lookup.items():
        norm_key = re.sub(r"[^a-z0-9 ]", "", key.lower())
        candidates[norm_key] = val

    candidate_list = list(candidates.keys())
    if not candidate_list:
        return "NotChecked"

    # Try all three scorers, take best
    best_score = 0
    best_match = None
    for scorer in [fuzz.ratio, fuzz.partial_ratio, fuzz.token_sort_ratio]:
        result = process.extractOne(
            norm_inst, candidate_list, scorer=scorer, score_cutoff=threshold
        )
        if result and result[1] > best_score:
            best_score = result[1]
            best_match = result[0]

    if best_match is None:
        return "NotChecked"

    status = candidates[best_match]
    return "True" if status in ("CONFIRMED", "CONFIRMED_VIA_RENAME",
                                 "CONFIRMED_VIA_WEB_RESOLVE") else "False"


# --- Public API ---

def search_nsf_awards(
    keywords: list[str],
    min_start_year: int = 2020,
    include_expired: bool = True,
    expired_lookback_years: int = 5,
) -> pd.DataFrame:
    """Query the NSF Award Search API for each keyword and return a deduplicated
    DataFrame with cross-reference to CS PhD verification.

    Args:
        keywords: List of search keywords (e.g. ["federated learning", "EEG"]).
        min_start_year: Only include awards starting on or after this year.
        include_expired: If False, only include currently active awards.
        expired_lookback_years: If include_expired=True, only include awards
            that expired within this many years of today.

    Returns:
        DataFrame with columns:
            keyword_matched, award_id, title, pi_name, co_pi_names,
            institution, state, start_date, exp_date, is_active,
            funds_obligated, abstract_snippet, has_verified_cs_phd
    """
    all_parsed = []

    for i, kw in enumerate(keywords):
        kw = kw.strip()
        if not kw:
            continue

        if i > 0:
            time.sleep(REQUEST_DELAY)

        raw = _query_nsf_api(kw)
        parsed = _parse_awards(raw, kw)
        all_parsed.extend(parsed)

    if not all_parsed:
        return pd.DataFrame()

    df = pd.DataFrame(all_parsed)

    # Filter by start year
    if min_start_year:
        df = df[df["start_date"].apply(
            lambda d: d is not None and d.year >= min_start_year
        )]

    # Filter by active/expired
    today = datetime.now().date()
    if not include_expired:
        df = df[df["is_active"] == True]
    else:
        cutoff = today - timedelta(days=expired_lookback_years * 365)
        df = df[df["exp_date"].apply(
            lambda d: d is not None and d >= cutoff
        )]

    if df.empty:
        return df

    # Deduplicate: group by award_id, merge keyword_matched
    deduped = (
        df.groupby("award_id", sort=False)
        .agg({
            "keyword_matched": lambda x: ", ".join(sorted(set(x))),
            "title": "first",
            "pi_name": "first",
            "co_pi_names": "first",
            "institution": "first",
            "state": "first",
            "start_date": "first",
            "exp_date": "first",
            "is_active": "first",
            "funds_obligated": "first",
            "abstract_snippet": "first",
        })
        .reset_index()
    )

    # Cross-reference against CS PhD verification
    phd_df = _load_phd_verification()
    if not phd_df.empty:
        phd_lookup = dict(zip(
            phd_df["matched_ipeds_name"].fillna(""),
            phd_df["status"].fillna("")
        ))
    else:
        phd_lookup = {}

    deduped["has_verified_cs_phd"] = deduped["institution"].apply(
        lambda inst: _match_institution_to_phd(inst, phd_lookup)
    )

    # Sort by funds_obligated descending
    deduped = deduped.sort_values("funds_obligated", ascending=False).reset_index(drop=True)

    return deduped


def format_currency(val: float) -> str:
    """Format a float as a currency string."""
    if val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val/1_000:.0f}K"
    return f"${val:,.0f}"


# --- Standalone test ---
if __name__ == "__main__":
    print("Testing NSF Award Search...")
    df = search_nsf_awards(
        keywords=["federated learning"],
        min_start_year=2020,
        include_expired=True,
        expired_lookback_years=5,
    )
    print(f"\nResults: {len(df)} awards")
    if not df.empty:
        print(f"Columns: {list(df.columns)}")
        print(f"\nUnique institutions: {df['institution'].nunique()}")
        print(f"Unique PIs: {df['pi_name'].nunique()}")
        print(f"Active awards: {df['is_active'].sum()}")
        print(f"\nCS PhD cross-match counts:")
        print(df["has_verified_cs_phd"].value_counts().to_string())
        print(f"\nTop 5 by funding:")
        for _, row in df.head(5).iterrows():
            print(f"  {row['pi_name']} @ {row['institution']}: "
                  f"{format_currency(row['funds_obligated'])} — {row['title'][:60]}...")
