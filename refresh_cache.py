#!/usr/bin/env python3
"""
Cache Refresh Script for PhD Finder.

Standalone script (run via cron or manually) that rebuilds the local data
caches the live app reads from. The live app NEVER calls external APIs —
this script does all the network I/O.

Usage:
    python refresh_cache.py                    # refresh all caches
    python refresh_cache.py --nsf-only         # refresh only NSF award cache
    python refresh_cache.py --iped-only        # refresh only IPEDS data
    python refresh_cache.py --keywords "keyword1,keyword2"  # specific NSF keywords

Schedule via cron (weekly Sunday 3am):
    0 3 * * 0 cd /path/to/cs_phd_check && python refresh_cache.py >> data/refresh.log 2>&1
"""

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Ensure project root is importable ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    DATA_DIR, CACHE_DIR, NSF_CACHE_DIR, DB_PATH, NSF_CSV_PATH,
    VERIFICATION_CSV, METADATA_PATH, write_metadata,
)

# ── Default NSF keywords for the fund-search cache ────────────────────────
DEFAULT_NSF_KEYWORDS = [
    "multimodal neuroimaging",
    "federated learning",
    "Alzheimer disease",
    "EEG classification",
    "medical image analysis",
    "MRI PET fusion",
    "machine learning",
    "deep learning",
    "natural language processing",
    "computer vision",
]


def refresh_ipeds():
    """Export needed IPEDS tables from the scipeds DuckDB to a local copy."""
    print("[IPEDS] Refreshing IPEDS data cache...")

    if not DB_PATH.exists():
        print(f"[IPEDS] WARNING: Source DB not found at {DB_PATH}")
        print("[IPEDS] Skipping IPEDS refresh. Set IPEDS_DB_PATH env var if needed.")
        return False

    import duckdb

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / "ipeds.duckdb"

    # If the output is the same as the source, skip
    if output_path.resolve() == DB_PATH.resolve():
        print(f"[IPEDS] Source and output are the same file, skipping copy.")
        return True

    print(f"[IPEDS] Source: {DB_PATH}")
    print(f"[IPEDS] Output: {output_path}")

    src = duckdb.connect(str(DB_PATH), read_only=True)

    # Remove old copy if it exists, then copy
    if output_path.exists():
        output_path.unlink()

    # Use DuckDB's copy to create a fresh local file with just the tables we need
    dst = duckdb.connect(str(output_path))

    for table in ["ipeds_directory_info", "ipeds_completions_a", "cip_info"]:
        print(f"[IPEDS]   Copying table: {table}...")
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            cols = [desc[0] for desc in src.execute(f"DESCRIBE {table}").fetchall()]
            dst.execute(f"DROP TABLE IF EXISTS {table}")
            # Build CREATE TABLE from the describe output
            col_defs = ", ".join(f'"{c[0]}" {c[1]}' for c in src.execute(f"DESCRIBE {table}").fetchall())
            dst.execute(f"CREATE TABLE {table} ({col_defs})")
            if rows:
                placeholders = ", ".join(["?"] * len(cols))
                dst.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            print(f"[IPEDS]   {table}: {len(rows)} rows")
        except Exception as e:
            print(f"[IPEDS]   WARNING: Failed to copy {table}: {e}")

    dst.close()
    src.close()

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"[IPEDS] Done. Output size: {size_mb:.1f} MB")
    return True


def refresh_nsf(keywords: list[str] | None = None):
    """Query the NSF API for the given keywords and save a deduplicated CSV."""
    print("[NSF] Refreshing NSF award cache...")

    from nsf_search import search_nsf_awards, _query_nsf_api, _parse_awards, _load_phd_verification, _match_institution_to_phd
    from rapidfuzz import fuzz, process
    import pandas as pd
    import re

    kw_list = keywords or DEFAULT_NSF_KEYWORDS
    print(f"[NSF] Keywords: {len(kw_list)}")

    all_parsed = []
    for i, kw in enumerate(kw_list):
        kw = kw.strip()
        if not kw:
            continue
        print(f"[NSF]   [{i+1}/{len(kw_list)}] Querying: {kw}...", end=" ", flush=True)
        try:
            raw = _query_nsf_api(kw)
            parsed = _parse_awards(raw, kw)
            all_parsed.extend(parsed)
            print(f"{len(parsed)} awards")
        except Exception as e:
            print(f"ERROR: {e}")
        if i < len(kw_list) - 1:
            time.sleep(1.0)

    if not all_parsed:
        print("[NSF] No awards found.")
        return

    df = pd.DataFrame(all_parsed)

    # Filter to last 5 years
    from datetime import timedelta
    cutoff = datetime.now().date() - timedelta(days=5 * 365)
    df = df[df["start_date"].apply(lambda d: d is not None and d.year >= 2020)]
    df = df[df["exp_date"].apply(lambda d: d is not None and d >= cutoff)]

    # Deduplicate
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

    # Cross-reference
    phd_df = _load_phd_verification()
    if not phd_df.empty:
        phd_lookup = dict(zip(
            phd_df["matched_ipeds_name"].fillna(""),
            phd_df["status"].fillna("")
        ))
    else:
        phd_lookup = {}

    def _match(inst):
        norm = re.sub(r"[^a-z0-9 ]", "", inst.lower())
        cands = {re.sub(r"[^a-z0-9 ]", "", k.lower()): v for k, v in phd_lookup.items()}
        if not cands:
            return "NotChecked"
        best = None
        best_s = 0
        for scorer in [fuzz.ratio, fuzz.partial_ratio, fuzz.token_sort_ratio]:
            r = process.extractOne(norm, list(cands.keys()), scorer=scorer, score_cutoff=85)
            if r and r[1] > best_s:
                best_s = r[1]
                best = r[0]
        if best is None:
            return "NotChecked"
        status = cands[best]
        return "True" if status in ("CONFIRMED", "CONFIRMED_VIA_RENAME", "CONFIRMED_VIA_WEB_RESOLVE") else "False"

    deduped["has_verified_phd"] = deduped["institution"].apply(_match)

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deduped.to_csv(NSF_CSV_PATH, index=False)

    print(f"[NSF] Done. {len(deduped)} unique awards saved to {NSF_CSV_PATH.name}")
    print(f"[NSF]   Institutions: {deduped['institution'].nunique()}")
    print(f"[NSF]   PIs: {deduped['pi_name'].nunique()}")
    print(f"[NSF]   Active: {deduped['is_active'].sum()}")


def update_metadata():
    """Write metadata about the refreshed data."""
    info = {
        "last_refreshed": datetime.now().isoformat(),
        "refresh_script": "refresh_cache.py",
    }

    # Count IPEDS records
    ipeds_path = DATA_DIR / "ipeds.duckdb"
    if ipeds_path.exists():
        import duckdb
        db = duckdb.connect(str(ipeds_path), read_only=True)
        try:
            n_inst = db.execute("SELECT COUNT(*) FROM ipeds_directory_info").fetchone()[0]
            n_comp = db.execute("SELECT COUNT(*) FROM ipeds_completions_a").fetchone()[0]
            info["ipeds_institutions"] = n_inst
            info["ipeds_completions_rows"] = n_comp
        except Exception:
            pass
        db.close()

    # Count NSF records
    if NSF_CSV_PATH.exists():
        import pandas as pd
        nsf_df = pd.read_csv(NSF_CSV_PATH)
        info["nsf_awards"] = len(nsf_df)
        info["nsf_institutions"] = nsf_df["institution"].nunique() if "institution" in nsf_df else 0

    # Count verification records
    if VERIFICATION_CSV.exists():
        import pandas as pd
        vdf = pd.read_csv(VERIFICATION_CSV)
        info["verification_records"] = len(vdf)

    write_metadata(info)
    print(f"[META] Updated {METADATA_PATH.name}")


def main():
    parser = argparse.ArgumentParser(description="Refresh PhD Finder data caches")
    parser.add_argument("--nsf-only", action="store_true", help="Only refresh NSF cache")
    parser.add_argument("--iped-only", action="store_true", help="Only refresh IPEDS cache")
    parser.add_argument("--keywords", type=str, help="Comma-separated NSF keywords")
    args = parser.parse_args()

    print(f"=== PhD Finder Cache Refresh — {datetime.now():%Y-%m-%d %H:%M} ===\n")

    do_ipeds = not args.nsf_only
    do_nsf = not args.iped_only

    if do_ipeds:
        refresh_ipeds()
        print()

    if do_nsf:
        keywords = args.keywords.split(",") if args.keywords else None
        refresh_nsf(keywords)
        print()

    update_metadata()
    print("\n=== Refresh complete ===")


if __name__ == "__main__":
    main()
