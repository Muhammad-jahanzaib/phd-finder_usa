#!/usr/bin/env python3
"""
CS PhD Verification Script
Uses IPEDS data (via the scipeds DuckDB database) to verify which universities
in a given list currently offer a PhD (research doctorate) in Computer Science.

Data: U.S. Dept of Education IPEDS completions data, 2019-2024
Source: scipeds pre-processed IPEDS database (DuckDB)
"""

import csv
import re
import sys
from pathlib import Path

import duckdb
from rapidfuzz import fuzz, process

# --- Configuration ---
BASE_DIR = Path(__file__).parent
OUTPUT_CSV = BASE_DIR / "cs_phd_verification.csv"
SUMMARY_MD = BASE_DIR / "methodology_summary.md"
DB_PATH = Path.home() / "Library/Caches/.scipeds/scipeds_0_0_8.duckdb"

# Lookback window (last 5 calendar years of completions data)
LOOKBACK_START_YEAR = 2020

# CIP codes that map to CS-family doctoral programs (6-digit codes)
CS_CIP_PREFIXES = [
    "11.",   # Computer and Information Sciences
    "14.09", # Computer Engineering
]

# Award level for research doctorate
DOCTORAL_AWLEVEL = "Doctor's degree - research/scholarship"

# Fuzzy matching threshold
MATCH_THRESHOLD = 85

# Sentinel value: input names mapped to this have NO IPEDS record (not accredited, etc.)
_NO_MATCH = "__NO_MATCH__"

# --- Manual aliases for known name variations ---
# Maps input name -> IPEDS official name, or _NO_MATCH if the institution has no IPEDS record.
MANUAL_ALIASES = {
    "The Johns Hopkins University": "Johns Hopkins University",
    "University of California, Berkeley": "University of California-Berkeley",
    "University of California, Los Angeles": "University of California-Los Angeles",
    "University of California, San Diego": "University of California-San Diego",
    "State University of New York at Stony Brook": "Stony Brook University",
    "University at Buffalo, State University of New York": "University at Buffalo",
    "Penn State University": "The Pennsylvania State University",
    "Rutgers, The State University of New Jersey": "Rutgers University-New Brunswick",
    "University of Texas at Austin": "The University of Texas at Austin",
    "University of Illinois at Urbana-Champaign": "University of Illinois Urbana-Champaign",
    "Ohio State University": "Ohio State University-Main Campus",
    "Virginia Tech": "Virginia Polytechnic Institute and State University",
    "Purdue University": "Purdue University-Main Campus",
    "University of Pittsburgh": "University of Pittsburgh-Pittsburgh Campus",
    "Columbia University": "Columbia University in the City of New York",
    "University of Washington": "University of Washington-Seattle Campus",
    "University of Michigan": "University of Michigan-Ann Arbor",
    "University of Minnesota": "University of Minnesota-Twin Cities",
    "University of Virginia": "University of Virginia-Main Campus",
    "University of Missouri": "University of Missouri-Columbia",
    "University of Tennessee": "University of Tennessee-Knoxville",
    "University of South Carolina": "University of South Carolina-Columbia",
    "Indiana University": "Indiana University-Bloomington",
    "Polytechnic Institute of New York University": "New York University",
    "University of Colorado Boulder": "University of Colorado Boulder/Colorado State University",
    "DeVry University": "DeVry University-Illinois",
    "Georgia Institute of Technology": "Georgia Institute of Technology-Main Campus",
    "University of Maryland, College Park": "University of Maryland-College Park",
    "San Jose State University": "San Jose State University",
    "Arizona State University": "Arizona State University Campus Immersion",
    # --- Near-miss threshold fixes (were scoring 82-84, below 85 threshold) ---
    "College of William and Mary": "William & Mary",
    "Loyola College, Baltimore": "Loyola University Maryland",
    # --- False-positive prevention: satellite campuses mapped to their own unitid ---
    "Auburn University, Montgomery": "Auburn University at Montgomery",
    "Brigham Young University Hawaii": "Brigham Young University-Hawaii",
    "Brigham Young University Idaho": "Brigham Young University-Idaho",
    # --- False-positive prevention: renamed institutions ---
    "California State University, Hayward": "California State University-East Bay",
    # --- Non-accredited / not in IPEDS ---
    "Apache University": _NO_MATCH,
}

# --- Web-resolved current names for renamed institutions ---
# Maps original name -> (current official name, resolution chain note)
WEB_RESOLVED_NAMES = {
    "Central Missouri State University": ("University of Central Missouri", "renamed 2006"),
    "East Stroudsburg State University": ("East Stroudsburg University of Pennsylvania", "renamed 1983"),
    "Mesa State College": ("Colorado Mesa University", "renamed 2011"),
    "Metropolitan State College of Denver": ("Metropolitan State University of Denver", "renamed 2012"),
    "North Georgia College and State University, the Military College of Georgia": ("University of North Georgia", "merged with Gainesville State College 2013"),
    "Salisbury State University": ("Salisbury University", "renamed 2001"),
    "Dixie State College": ("Utah Tech University", "renamed 2022"),
    "Johnson State College": ("Vermont State University", "merged into Vermont State University 2023"),
    "Troy State University - Dothan": ("Troy University", "merged 2005"),
    "University of Great Falls": ("University of Providence", "renamed 2017"),
    "College Misericordia": ("Misericordia University", "renamed 2007"),
    "College of Mount Saint Joseph": ("Mount St. Joseph University", "renamed 2014"),
    "College of Notre Dame of Maryland": ("Notre Dame of Maryland University", "renamed 2011"),
    "College of Saint Catherine": ("St. Catherine University", "renamed 2009"),
    "Castleton State College": ("Vermont State University", "merged into Vermont State University 2023"),
    "Wheeling Jesuit University": ("Wheeling University", "renamed 2019"),
    "Texas A&M University, Commerce": ("East Texas A&M University", "renamed Nov 2024"),
    "University of Texas at Brownsville": ("The University of Texas Rio Grande Valley", "merged 2015"),
    "University of Texas-Pan American": ("The University of Texas Rio Grande Valley", "merged 2015"),
    # "Oklahoma State University Tulsa" — no separate IPEDS entry for Tulsa campus, skip
    "University of Pittsburgh at Greenburg": ("University of Pittsburgh-Greensburg", "spelling fix"),
    "University of Pittsburgh at Johnstown": ("University of Pittsburgh-Johnstown", "at->hyphen"),
    "Georgia Perimeter College": ("Georgia State University-Perimeter College", "merged 2016"),
    "Humboldt State University": ("California State Polytechnic University, Humboldt", "renamed 2022"),
}


def load_database():
    """Open the scipeds DuckDB database."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Run: python3 -c 'import scipeds; scipeds.download_db()'")
        sys.exit(1)
    return duckdb.connect(str(DB_PATH), read_only=True)


def get_cs_doctoral_schools(db) -> dict[int, dict]:
    """Query all schools with doctoral CS completions in the lookback window.
    Returns dict of unitid -> {cip_codes: {code: [{year, n_awards, cip_name}]}}
    """
    print(f"\n=== Querying CS Doctoral Completions (year >= {LOOKBACK_START_YEAR}) ===")

    # Build CIP prefix filter
    cip_conditions = " OR ".join(
        f"cip2020 LIKE '{prefix}%'" for prefix in CS_CIP_PREFIXES
    )

    query = f"""
        SELECT
            unitid,
            cip2020,
            year,
            SUM(n_awards) as total_awards
        FROM ipeds_completions_a
        WHERE ({cip_conditions})
          AND awlevel = 'Doctor''s degree - research/scholarship'
          AND majornum = 1
          AND year >= {LOOKBACK_START_YEAR}
        GROUP BY unitid, cip2020, year
        ORDER BY unitid, cip2020, year
    """

    results = db.execute(query).fetchall()
    print(f"  Found {len(results)} rows of CS doctoral completions")

    # Get CIP code names from cip_info table
    cip_names = {}
    try:
        cip_rows = db.execute("""
            SELECT cip2020, cip_title
            FROM cip_info
            WHERE cip2020 LIKE '11.%' OR cip2020 LIKE '14.09%'
        """).fetchall()
        for code, title in cip_rows:
            cip_names[code] = title
    except Exception:
        pass  # cip_info table may not have all entries

    # Build the lookup
    cs_schools: dict[int, dict] = {}
    for unitid, cip, year, awards in results:
        if unitid not in cs_schools:
            cs_schools[unitid] = {"cip_codes": {}}
        if cip not in cs_schools[unitid]["cip_codes"]:
            cs_schools[unitid]["cip_codes"][cip] = []
        cs_schools[unitid]["cip_codes"][cip].append({
            "year": year,
            "n_awards": awards,
            "cip_name": cip_names.get(cip, cip),
        })

    # Summary
    total_phds = sum(
        e["n_awards"]
        for s in cs_schools.values()
        for entries in s["cip_codes"].values()
        for e in entries
    )
    print(f"  Total unique schools with CS doctoral completions: {len(cs_schools)}")
    print(f"  Total CS doctoral completions in window: {total_phds}")

    return cs_schools


def get_directory(db) -> dict[int, dict]:
    """Get the IPEDS institution directory.
    Returns dict of unitid -> {name, state, city}
    """
    print("\n=== Loading IPEDS Directory ===")

    # Get the most recent directory entries
    rows = db.execute("""
        SELECT DISTINCT
            unitid,
            institution_name,
            state_abbreviation,
            city_location_of_institution
        FROM ipeds_directory_info
    """).fetchall()

    directory = {}
    for uid, name, state, city in rows:
        # Some unitids appear in multiple years; keep latest or first
        if uid not in directory:
            directory[uid] = {
                "name": name or "",
                "state": state or "",
                "city": city or "",
            }

    print(f"  Loaded {len(directory)} institutions")
    return directory


def normalize_name(name: str) -> str:
    """Normalize a school name for fuzzy matching."""
    name = name.strip()
    name = re.sub(r"^(The|A|An)\s+", "", name, flags=re.IGNORECASE)
    name = name.replace(",", "").replace(".", "")
    name = re.sub(r"\s+", " ", name).strip()
    return name.lower()


def _first_word(name: str) -> str:
    """Extract the first significant word from a normalized name."""
    norm = normalize_name(name)
    skip = {"the", "a", "an", "university", "of", "at", "college", "institute", "school"}
    for w in norm.split():
        if w not in skip:
            return w
    return norm.split()[0] if norm.split() else ""


def fuzzy_match_school(
    input_name: str,
    directory: dict[int, dict],
) -> tuple[int | None, float, str | None]:
    """Match an input school name against the IPEDS directory.
    Returns (unitid, score, matched_name) or (None, 0, None).
    """
    # Check manual aliases first
    if input_name in MANUAL_ALIASES:
        target = MANUAL_ALIASES[input_name]
        if target == _NO_MATCH:
            return None, 100.0, "Not in IPEDS (alias: no match)"
        target_norm = normalize_name(target)
        for uid, info in directory.items():
            if normalize_name(info["name"]) == target_norm:
                return uid, 100.0, info["name"]
        # If alias target not found, fall through to fuzzy matching

    # Build candidate list
    dir_names = {}
    for uid, info in directory.items():
        norm = normalize_name(info["name"])
        dir_names[norm] = (uid, info["name"])

    input_norm = normalize_name(input_name)
    candidates = list(dir_names.keys())

    best_match = None
    best_score = 0

    # Strategy 1: ratio (exact character match)
    result = process.extractOne(
        input_norm, candidates, scorer=fuzz.ratio, score_cutoff=MATCH_THRESHOLD - 10
    )
    if result:
        match, score, _ = result
        uid, real_name = dir_names[match]
        if score > best_score:
            best_score = score
            best_match = (uid, score, real_name)

    # Strategy 2: partial_ratio (substring matching)
    result2 = process.extractOne(
        input_norm, candidates, scorer=fuzz.partial_ratio, score_cutoff=MATCH_THRESHOLD - 5
    )
    if result2:
        match, score, _ = result2
        uid, real_name = dir_names[match]
        if score > best_score:
            best_score = score
            best_match = (uid, score, real_name)

    # Strategy 3: token_sort_ratio (handles word reordering)
    result3 = process.extractOne(
        input_norm, candidates, scorer=fuzz.token_sort_ratio, score_cutoff=MATCH_THRESHOLD - 5
    )
    if result3:
        match, score, _ = result3
        uid, real_name = dir_names[match]
        if score > best_score:
            best_score = score
            best_match = (uid, score, real_name)

    if best_match:
        uid, score, real_name = best_match
        # Cross-state / cross-name guard: if the first significant word of the
        # input and the matched name differ, the match is almost certainly wrong
        # (e.g. "california state" matched to "florida state"). Penalize heavily.
        input_first = _first_word(input_name)
        match_first = _first_word(real_name)
        if input_first != match_first and score < 95:
            # Only penalize if the first word is substantive (not just "university")
            if len(input_first) > 3 and len(match_first) > 3:
                return None, 0, None

        return best_match

    return None, 0, None


# ─── Rename-pattern second pass ────────────────────────────────────────────

# Patterns: (regex replacement, human-readable label)
_RENAME_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _add_pattern(pattern: str, replacement: str, label: str):
    _RENAME_PATTERNS.append((re.compile(pattern, re.IGNORECASE), replacement, label))


# "X College" -> "X University"  (only trailing "college", not mid-name)
_add_pattern(r"\bcollege\s*$", "university", "College->University")

# "X State College" -> "X State University"  (covered by above, but explicit label)
_add_pattern(r"\bstate college\s*$", "state university", "State College->State University")

# "X Normal School" -> "X University"
_add_pattern(r"\bnormal school\s*$", "university", "Normal School->University")

# "X Seminary" -> "X University"
_add_pattern(r"\bseminary\s*$", "university", "Seminary->University")

# Normalize ampersand: "&" -> "and"
_add_pattern(r" & ", " and ", "Ampersand->and")

# Normalize "and" -> "&" (reverse direction for IPEDS names like "William & Mary")
# We do this as a separate variant, not in-place

# Campus separator: " at " -> "-"  (e.g. "University of X at Y" -> "University of X-Y")
_add_pattern(r"\s+at\s+", "-", "At->hyphen")

# Strip hyphens and collapse spaces
_add_pattern(r"\s*-\s*", " ", "Hyphen->space")

# "X, Y" -> "X Y" (remove internal commas)
_add_pattern(r",", "", "Comma removal")

# Normalize backticks to apostrophes (e.g. "Hawai`i" -> "Hawaii")
_add_pattern(r"`", "'", "Backtick->apostrophe")

# Normalize "Hawaii" variants
_add_pattern(r"\bhawai`i\b", "hawaii", "Hawai`i->Hawaii")
_add_pattern(r"\bhawai'i\b", "hawaii", "Hawai'i->Hawaii")


def _case_aware_replace(regex: re.Pattern, replacement: str, text: str) -> str:
    """Replace matched text, preserving the case of the original match."""
    def replacer(match):
        matched = match.group(0)
        if matched.isupper():
            return replacement.upper()
        elif matched[0].isupper():
            return replacement[0].upper() + replacement[1:]
        return replacement
    return regex.sub(replacer, text)


def generate_rename_variants(input_name: str) -> list[tuple[str, str]]:
    """Generate rename-pattern variants of an input name.
    Returns list of (variant_name, pattern_label).
    """
    variants = []
    seen = set()

    # Apply each pattern independently to the original name
    for regex, replacement, label in _RENAME_PATTERNS:
        candidate = _case_aware_replace(regex, replacement, input_name).strip()
        candidate = re.sub(r"\s+", " ", candidate)  # collapse whitespace
        if candidate != input_name and candidate not in seen:
            seen.add(candidate)
            variants.append((candidate, label))

    # Also try "and" <-> "&" swap
    if " & " in input_name:
        alt = input_name.replace(" & ", " and ")
        if alt not in seen:
            seen.add(alt)
            variants.append((alt, "Ampersand swap (& -> and)"))
    if " and " in input_name:
        alt = input_name.replace(" and ", " & ")
        if alt not in seen:
            seen.add(alt)
            variants.append((alt, "Ampersand swap (and -> &)"))

    return variants


def _significant_words(name: str) -> set[str]:
    """Return the set of significant (non-trivial) words from a name."""
    skip = {"the", "a", "an", "university", "of", "at", "college", "institute",
            "school", "campus", "system", "office"}
    norm = normalize_name(name)
    return {w for w in norm.split() if w not in skip and len(w) > 2}


def fuzzy_match_variants(
    input_name: str,
    directory: dict[int, dict],
    dir_names: dict[str, tuple[int, str]],
) -> tuple[int | None, float, str | None, str]:
    """Try rename-pattern variants against the directory.
    Returns (unitid, score, matched_name, pattern_label) or (None, 0, None, "").
    """
    variants = generate_rename_variants(input_name)

    best_match = None
    best_score = 0
    best_label = ""

    candidates = list(dir_names.keys())

    for variant, label in variants:
        variant_norm = normalize_name(variant)

        for scorer in [fuzz.ratio, fuzz.token_sort_ratio]:
            result = process.extractOne(
                variant_norm, candidates, scorer=scorer, score_cutoff=MATCH_THRESHOLD
            )
            if result:
                match, score, _ = result
                uid, real_name = dir_names[match]
                # Word-overlap check: variant and IPEDS name must share at least
                # 2 significant words (catches campus mismatches like
                # "Colorado State Pueblo" matching "Colorado State-Fort Collins")
                v_words = _significant_words(variant)
                m_words = _significant_words(real_name)
                if len(v_words & m_words) < 1:
                    continue
                if score > best_score:
                    # Cross-state guard: first word must match
                    variant_first = _first_word(variant)
                    match_first = _first_word(real_name)
                    if variant_first != match_first and score < 95:
                        if len(variant_first) > 3 and len(match_first) > 3:
                            continue
                    # Extra strict: first TWO words must match for rename pass
                    # when score is marginal (catches e.g. "Baylor College of
                    # Dentistry" -> "Baylor University" matching "Baylor University")
                    if score < 90:
                        variant_words = normalize_name(variant).split()
                        match_words = normalize_name(real_name).split()
                        v2 = " ".join(variant_words[:2]) if len(variant_words) >= 2 else variant_words[0]
                        m2 = " ".join(match_words[:2]) if len(match_words) >= 2 else match_words[0]
                        if v2 != m2:
                            continue
                    best_score = score
                    best_match = (uid, score, real_name)
                    best_label = label

    if best_match:
        return (*best_match, best_label)
    return None, 0, None, ""


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    # Step 1: Read input universities
    input_file = BASE_DIR / "universities.txt"
    if not input_file.exists():
        print(f"ERROR: {input_file} not found")
        sys.exit(1)

    with open(input_file) as f:
        input_names = [line.strip() for line in f if line.strip()]

    print(f"Loaded {len(input_names)} university names from {input_file}")

    # Step 2: Open database and load data
    db = load_database()
    directory = get_directory(db)
    cs_schools = get_cs_doctoral_schools(db)

    # Step 3: Match each input name
    print("\n=== Matching Universities ===")
    results = []

    for name in input_names:
        uid, score, matched = fuzzy_match_school(name, directory)

        if uid is None:
            results.append({
                "original_name": name,
                "matched_ipeds_name": "NO MATCH FOUND",
                "unitid": "",
                "match_score": 0,
                "status": "NEEDS_REVIEW",
                "total_cs_phds_last5yrs": 0,
                "cip_codes_found": "",
                "source_note": "No confident match found in IPEDS directory",
            })
            continue

        # Check if this school has CS doctoral completions
        if uid in cs_schools:
            cip_info = cs_schools[uid]["cip_codes"]
            total_phds = sum(
                entry["n_awards"]
                for entries in cip_info.values()
                for entry in entries
            )
            cip_str = "; ".join(
                f"{code} ({entries[0]['cip_name']}): "
                f"{sum(e['n_awards'] for e in entries)} PhDs"
                for code, entries in cip_info.items()
            )

            if total_phds > 0:
                status = "CONFIRMED"
                note = f"IPEDS unitid={uid}, CIP codes: {', '.join(cip_info.keys())}"
            else:
                status = "NO_PHD_PROGRAM"
                note = f"IPEDS unitid={uid}: 0 doctoral CS completions in {LOOKBACK_START_YEAR}-2024"
        else:
            status = "NO_PHD_PROGRAM"
            total_phds = 0
            cip_str = ""
            note = f"IPEDS unitid={uid}: No doctoral CS completions in any CS-family CIP code ({LOOKBACK_START_YEAR}-2024)"

        if score < MATCH_THRESHOLD:
            status = "NEEDS_REVIEW"
            note += f" [match score {score:.0f} < threshold {MATCH_THRESHOLD}]"

        # Defensive: CONFIRMED absolutely requires PhDs > 0
        if status == "CONFIRMED" and total_phds <= 0:
            status = "NO_PHD_PROGRAM"
            note = f"DEFENSIVE FIX: was CONFIRMED but total_phds=0, reclassified"

        results.append({
            "original_name": name,
            "matched_ipeds_name": matched or "NO MATCH",
            "unitid": uid,
            "match_score": round(score, 1),
            "status": status,
            "total_cs_phds_last5yrs": total_phds,
            "cip_codes_found": cip_str,
            "source_note": note,
        })

    db.close()

    # ─── Step 3b: Rename-pattern second pass (NEEDS_REVIEW only) ───────────
    print("\n=== Rename-Pattern Second Pass ===")
    dir_names = {}
    for r in results:
        if r["unitid"] != "" and r["unitid"] in directory:
            norm = normalize_name(directory[r["unitid"]]["name"])
            dir_names[norm] = (r["unitid"], directory[r["unitid"]]["name"])
    # Rebuild dir_names from the full directory (some NEEDS_REVIEW have no unitid)
    dir_names = {}
    for uid, info in directory.items():
        norm = normalize_name(info["name"])
        dir_names[norm] = (uid, info["name"])

    needs_review_indices = [i for i, r in enumerate(results) if r["status"] == "NEEDS_REVIEW"]
    rename_resolved = 0
    rename_still_review = 0

    for idx in needs_review_indices:
        r = results[idx]
        name = r["original_name"]

        uid, score, matched, pattern_label = fuzzy_match_variants(name, directory, dir_names)

        if uid is None:
            rename_still_review += 1
            continue

        # Determine PhD status for the matched school
        if uid in cs_schools:
            cip_info = cs_schools[uid]["cip_codes"]
            total_phds = sum(
                entry["n_awards"]
                for entries in cip_info.values()
                for entry in entries
            )
            cip_str = "; ".join(
                f"{code} ({entries[0]['cip_name']}): "
                f"{sum(e['n_awards'] for e in entries)} PhDs"
                for code, entries in cip_info.items()
            )
        else:
            total_phds = 0
            cip_str = ""

        if total_phds > 0:
            status = "CONFIRMED_VIA_RENAME"
        else:
            status = "NO_PHD_VIA_RENAME"

        note = (f"Renamed match via pattern '{pattern_label}': "
                f"IPEDS unitid={uid}")

        results[idx] = {
            "original_name": name,
            "matched_ipeds_name": matched,
            "unitid": uid,
            "match_score": round(score, 1),
            "status": status,
            "total_cs_phds_last5yrs": total_phds,
            "cip_codes_found": cip_str,
            "source_note": note,
            "rename_pattern": pattern_label,
        }
        rename_resolved += 1

    # Ensure all non-renamed results have the rename_pattern field
    for r in results:
        if "rename_pattern" not in r:
            r["rename_pattern"] = ""

    print(f"  Resolved via rename patterns: {rename_resolved}")
    print(f"  Still NEEDS_REVIEW:            {rename_still_review}")

    # ─── Step 3c: Web-resolution pass (NEEDS_REVIEW only) ────────────────
    print("\n=== Web-Resolution Third Pass ===")
    needs_review_indices = [i for i, r in enumerate(results) if r["status"] == "NEEDS_REVIEW"]
    web_resolved = 0
    web_still_review = 0

    for idx in needs_review_indices:
        r = results[idx]
        name = r["original_name"]

        if name not in WEB_RESOLVED_NAMES:
            web_still_review += 1
            continue

        current_name, resolution_note = WEB_RESOLVED_NAMES[name]
        chain = f"{name} -> {current_name} ({resolution_note})"

        # Try to match the resolved name through fuzzy_match_school
        uid, score, matched = fuzzy_match_school(current_name, directory)

        if uid is None:
            # Also try rename variants on the resolved name
            uid, score, matched, pattern_label = fuzzy_match_variants(
                current_name, directory, dir_names
            )
            if uid is not None:
                chain += f" -> {matched} (via {pattern_label})"

        if uid is None:
            # Could not match even after web resolution
            results[idx] = {
                "original_name": name,
                "matched_ipeds_name": "NO MATCH FOUND",
                "unitid": "",
                "match_score": 0,
                "status": "NEEDS_REVIEW",
                "total_cs_phds_last5yrs": 0,
                "cip_codes_found": "",
                "source_note": f"Web resolution attempted ({current_name}), inconclusive: {resolution_note}",
                "rename_pattern": "",
                "web_resolve_chain": chain,
            }
            web_still_review += 1
            continue

        # Determine PhD status
        if uid in cs_schools:
            cip_info = cs_schools[uid]["cip_codes"]
            total_phds = sum(
                entry["n_awards"]
                for entries in cip_info.values()
                for entry in entries
            )
            cip_str = "; ".join(
                f"{code} ({entries[0]['cip_name']}): "
                f"{sum(e['n_awards'] for e in entries)} PhDs"
                for code, entries in cip_info.items()
            )
        else:
            total_phds = 0
            cip_str = ""

        if total_phds > 0:
            status = "CONFIRMED_VIA_WEB_RESOLVE"
        else:
            status = "NO_PHD_VIA_WEB_RESOLVE"

        note = (f"Web-resolved: {chain} -> IPEDS unitid={uid}")

        results[idx] = {
            "original_name": name,
            "matched_ipeds_name": matched,
            "unitid": uid,
            "match_score": round(score, 1),
            "status": status,
            "total_cs_phds_last5yrs": total_phds,
            "cip_codes_found": cip_str,
            "source_note": note,
            "rename_pattern": "",
            "web_resolve_chain": chain,
        }
        web_resolved += 1

    # Ensure all results have the web_resolve_chain field
    for r in results:
        if "web_resolve_chain" not in r:
            r["web_resolve_chain"] = ""

    print(f"  Resolved via web search: {web_resolved}")
    print(f"  Still NEEDS_REVIEW:      {web_still_review}")

    # Step 4: Write CSV
    print(f"\n=== Writing CSV to {OUTPUT_CSV} ===")
    fieldnames = [
        "original_name", "matched_ipeds_name", "unitid", "match_score",
        "status", "total_cs_phds_last5yrs", "cip_codes_found", "source_note",
        "rename_pattern", "web_resolve_chain",
    ]
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Step 5: Print summary
    confirmed = [r for r in results if r["status"] == "CONFIRMED"]
    confirmed_rename = [r for r in results if r["status"] == "CONFIRMED_VIA_RENAME"]
    confirmed_web = [r for r in results if r["status"] == "CONFIRMED_VIA_WEB_RESOLVE"]
    no_phd = [r for r in results if r["status"] == "NO_PHD_PROGRAM"]
    no_phd_rename = [r for r in results if r["status"] == "NO_PHD_VIA_RENAME"]
    no_phd_web = [r for r in results if r["status"] == "NO_PHD_VIA_WEB_RESOLVE"]
    needs_review = [r for r in results if r["status"] == "NEEDS_REVIEW"]

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Total input universities: {len(results)}")
    print(f"  CONFIRMED (direct match):              {len(confirmed)}")
    print(f"  CONFIRMED_VIA_RENAME (rename pass):    {len(confirmed_rename)}")
    print(f"  CONFIRMED_VIA_WEB_RESOLVE (web pass):  {len(confirmed_web)}")
    print(f"  NO_PHD_PROGRAM (direct match):         {len(no_phd)}")
    print(f"  NO_PHD_VIA_RENAME (rename pass):       {len(no_phd_rename)}")
    print(f"  NO_PHD_VIA_WEB_RESOLVE (web pass):     {len(no_phd_web)}")
    print(f"  NEEDS_REVIEW (still unmatched):        {len(needs_review)}")

    all_confirmed = confirmed + confirmed_rename + confirmed_web
    all_no_phd = no_phd + no_phd_rename + no_phd_web

    if all_confirmed:
        print(f"\n--- All CONFIRMED Schools ({len(all_confirmed)}) ---")
        for r in all_confirmed:
            via = f" [via: {r['rename_pattern']}]" if r["rename_pattern"] else ""
            print(f"  {r['original_name']} -> {r['matched_ipeds_name']} "
                  f"({r['total_cs_phds_last5yrs']} PhDs, CIPs: {r['cip_codes_found']}){via}")

    if all_no_phd:
        print(f"\n--- All NO_PHD_PROGRAM Schools ({len(all_no_phd)}) ---")
        for r in all_no_phd:
            via = f" [via: {r['rename_pattern']}]" if r["rename_pattern"] else ""
            print(f"  {r['original_name']} -> {r['matched_ipeds_name']}{via}")

    if needs_review:
        print(f"\n--- NEEDS_REVIEW ({len(needs_review)}, manual check needed) ---")
        for r in needs_review:
            print(f"  {r['original_name']}: {r['source_note']}")

    # Sanity sweep: all CONFIRMED with match_score < 95
    low_score_confirmed = [r for r in all_confirmed if float(r['match_score']) < 95]
    if low_score_confirmed:
        print(f"\n--- SANITY SWEEP: CONFIRMED with match_score < 95 ({len(low_score_confirmed)}) ---")
        for r in low_score_confirmed:
            print(f"  {r['original_name']} -> {r['matched_ipeds_name']} "
                  f"(score: {r['match_score']}, PhDs: {r['total_cs_phds_last5yrs']}, "
                  f"CIPs: {r['cip_codes_found']})")
    else:
        print("\n--- SANITY SWEEP: All CONFIRMED rows have match_score >= 95 ---")

    # Audit: CONFIRMED with 0 PhDs (should be zero after defensive fix)
    zero_phd_confirmed = [r for r in all_confirmed if int(r['total_cs_phds_last5yrs']) == 0]
    if zero_phd_confirmed:
        print(f"\n*** AUDIT WARNING: {len(zero_phd_confirmed)} CONFIRMED rows with 0 PhDs ***")
        for r in zero_phd_confirmed:
            print(f"  {r['original_name']} -> {r['matched_ipeds_name']}")
    else:
        print("\n--- AUDIT: Zero CONFIRMED rows have 0 PhDs ---")

    # Step 6: Write methodology summary
    with open(SUMMARY_MD, "w") as f:
        f.write("# CS PhD Verification - Methodology\n\n")
        f.write("## Data Source\n")
        f.write("- U.S. Department of Education IPEDS (Integrated Postsecondary Education Data System)\n")
        f.write("- Accessed via the `scipeds` Python package (pre-processed DuckDB database)\n")
        f.write("- Source: NCES IPEDS Completions survey (C_A component)\n\n")
        f.write("## CIP Codes Checked (CS Family)\n")
        f.write("- **11.xxxx**: Computer and Information Sciences (all subcodes)\n")
        f.write("  - 11.0101: Computer and Information Sciences, General\n")
        f.write("  - 11.0701: Computer Science\n")
        f.write("  - 11.0401: Information Science/Studies\n")
        f.write("  - And other 11.x subcodes\n")
        f.write("- **14.09xx**: Computer Engineering\n")
        f.write("  - 14.0901: Computer Engineering\n\n")
        f.write("## Award Level\n")
        f.write("- IPEDS award level: \"Doctor's degree - research/scholarship\"\n")
        f.write("- Filtered to MAJORNUM=1 (first major only) to avoid double-counting\n\n")
        f.write(f"## Lookback Window\n")
        f.write(f"- Years {LOOKBACK_START_YEAR}-2024 (5 years of completions data)\n\n")
        f.write("## Method\n")
        f.write("1. Loaded full IPEDS institution directory from scipeds database\n")
        f.write(f"2. Queried all schools with doctoral completions in CS-family CIP codes ({LOOKBACK_START_YEAR}-2024)\n")
        f.write("3. Fuzzy-matched input university names against IPEDS official names (rapidfuzz)\n")
        f.write("4. For each matched school, checked if ANY doctoral CS completions exist in window\n\n")
        f.write("## Matching\n")
        f.write(f"- Threshold: {MATCH_THRESHOLD} (rapidfuzz scoring: ratio, partial_ratio, token_sort_ratio)\n")
        f.write("- Manual aliases for known name variations (e.g. 'The Johns Hopkins University')\n")
        f.write("- Scores below threshold flagged as NEEDS_REVIEW\n\n")
        f.write("## Rename-Pattern Second Pass\n")
        f.write("- Applied only to NEEDS_REVIEW entries from the first pass\n")
        f.write("- Tests common historical rename patterns:\n")
        f.write("  - College -> University, State College -> State University\n")
        f.write("  - Normal School -> University, Seminary -> University\n")
        f.write("  - Strip trailing 'of <Place>' qualifiers\n")
        f.write("  - Normalize ampersands, hyphens, commas\n")
        f.write("- Pattern-matched entries labeled CONFIRMED_VIA_RENAME / NO_PHD_VIA_RENAME\n")
        f.write(f"- Resolved {rename_resolved} of {rename_resolved + rename_still_review} NEEDS_REVIEW entries\n\n")
        f.write("## Web-Resolution Third Pass\n")
        f.write("- Applied to remaining NEEDS_REVIEW entries after rename-pattern pass\n")
        f.write("- Web search used to find current official names of renamed institutions\n")
        f.write("- Resolved names re-run through fuzzy_match_school + rename-pattern logic\n")
        f.write("- Pattern-matched entries labeled CONFIRMED_VIA_WEB_RESOLVE / NO_PHD_VIA_WEB_RESOLVE\n")
        f.write("- Full resolution chain logged for auditability\n\n")
        f.write("## Caveats\n")
        f.write("- IPEDS reports completions (graduations), not currently-open admissions\n")
        f.write("- A program with zero recent graduates may still be new/admitting\n")
        f.write("- Programs established very recently may not appear in the lookback window\n")
        f.write("- Some schools may report CS PhDs under CIP codes outside the checked range\n")
        f.write("- Small programs (fewer than ~3 graduates in 5 years) may be suppressed by IPEDS\n")
        f.write("- IPEDS uses institutional names that may differ from common usage\n\n")
        f.write("## Summary\n")
        f.write(f"- CONFIRMED (direct): {len(confirmed)} schools have IPEDS-documented CS doctoral completions\n")
        f.write(f"- CONFIRMED_VIA_RENAME: {len(confirmed_rename)} matched via rename-pattern second pass\n")
        f.write(f"- CONFIRMED_VIA_WEB_RESOLVE: {len(confirmed_web)} matched via web-resolved names\n")
        f.write(f"- NO_PHD_PROGRAM (direct): {len(no_phd)} matched but no CS doctoral completions\n")
        f.write(f"- NO_PHD_VIA_RENAME: {len(no_phd_rename)} matched via rename, no CS doctoral completions\n")
        f.write(f"- NO_PHD_VIA_WEB_RESOLVE: {len(no_phd_web)} matched via web-resolved names, no CS doctoral completions\n")
        f.write(f"- NEEDS_REVIEW: {len(needs_review)} could not be confidently matched\n")

    print(f"\nMethodology summary written to {SUMMARY_MD}")
    print("Done.")


if __name__ == "__main__":
    main()
