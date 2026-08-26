#!/usr/bin/env python3
"""
PhD Finder — Search U.S. doctoral programs and funded research.

Streamlit web app for searching IPEDS doctoral completions data and
NSF-funded research awards. Deployable to Streamlit Community Cloud
or any Docker-capable host.
"""

import csv
import io
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
from rapidfuzz import fuzz, process

from config import (
    DB_PATH, NSF_CSV_PATH, VERIFICATION_CSV, DATA_DIR,
    MAX_QUERY_LENGTH, MAX_KEYWORDS, sanitize_input, get_metadata,
)

GA4_ID = "G-V1BCYS79RM"
USAGE_LOG = DATA_DIR / "usage_log.csv"
USAGE_LOG_FIELDS = ["timestamp", "tab", "query", "num_results"]


# ── Database helpers ──────────────────────────────────────────────────────

def _log_search(tab: str, query: str, num_results: int):
    """Append an anonymous search record to usage_log.csv.

    Logs only: timestamp, which tab, the query text, and result count.
    No IP addresses, session IDs, or user agents are recorded.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = USAGE_LOG.exists()
    with open(USAGE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=USAGE_LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tab": tab,
            "query": query,
            "num_results": num_results,
        })

@st.cache_resource
def get_db():
    """Open the IPEDS DuckDB database (cached across reruns)."""
    if not DB_PATH.exists():
        return None
    return duckdb.connect(str(DB_PATH), read_only=True)


@st.cache_data
def get_all_cip_codes(_db):
    """Fetch all CIP codes with their titles."""
    rows = _db.execute("""
        SELECT cip2020, cip_title
        FROM cip_info
        WHERE cip2020 IS NOT NULL AND cip_title IS NOT NULL
    """).fetchall()
    return {code: title for code, title in rows}


@st.cache_data
def get_directory(_db):
    """Fetch the IPEDS institution directory."""
    rows = _db.execute("""
        SELECT DISTINCT
            unitid,
            institution_name,
            state_abbreviation,
            city_location_of_institution
        FROM ipeds_directory_info
    """).fetchall()
    return {uid: {"name": name or "", "state": state or "", "city": city or ""}
            for uid, name, state, city in rows}


def query_doctoral_completions(_db, cip_prefixes, year_start, year_end,
                                awlevel="Doctor's degree - research/scholarship"):
    """Query doctoral completions for given CIP prefixes and year range."""
    cip_conditions = " OR ".join(
        f"cip2020 LIKE '{prefix}%'" for prefix in cip_prefixes
    )
    query = f"""
        SELECT
            unitid,
            cip2020,
            year,
            SUM(n_awards) as total_awards
        FROM ipeds_completions_a
        WHERE ({cip_conditions})
          AND awlevel = ?
          AND majornum = 1
          AND year >= {year_start}
          AND year <= {year_end}
        GROUP BY unitid, cip2020, year
        ORDER BY unitid, cip2020, year
    """
    return _db.execute(query, [awlevel]).fetchall()


def query_institution_completions(_db, unitid, year_start, year_end,
                                   awlevel="Doctor's degree - research/scholarship"):
    """Query all doctoral completions for a specific institution."""
    query = f"""
        SELECT
            cip2020,
            year,
            SUM(n_awards) as total_awards
        FROM ipeds_completions_a
        WHERE unitid = {unitid}
          AND awlevel = ?
          AND majornum = 1
          AND year >= {year_start}
          AND year <= {year_end}
        GROUP BY cip2020, year
        ORDER BY cip2020, year
    """
    return _db.execute(query, [awlevel]).fetchall()


# ── Fuzzy matching ────────────────────────────────────────────────────────

def fuzzy_match_cip(keyword, cip_codes, threshold=60):
    """Fuzzy match a keyword against CIP code titles.
    Returns list of (code, title, score) sorted by score descending.
    """
    keyword_lower = keyword.lower().strip()
    candidates = {f"{code} {title}": (code, title) for code, title in cip_codes.items()}
    candidate_list = list(candidates.keys())

    results = []
    matches = process.extract(
        keyword_lower, candidate_list,
        scorer=fuzz.ratio, score_cutoff=threshold, limit=20
    )
    for match_str, score, _ in matches:
        code, title = candidates[match_str]
        results.append((code, title, score))

    matches2 = process.extract(
        keyword_lower, candidate_list,
        scorer=fuzz.partial_ratio, score_cutoff=threshold, limit=20
    )
    for match_str, score, _ in matches2:
        code, title = candidates[match_str]
        if not any(r[0] == code for r in results):
            results.append((code, title, score))

    seen = set()
    unique = []
    for code, title, score in sorted(results, key=lambda x: -x[2]):
        if code not in seen:
            seen.add(code)
            unique.append((code, title, score))

    return unique


def fuzzy_match_institution(name, directory, threshold=75):
    """Fuzzy match an institution name against the IPEDS directory."""
    name_lower = name.lower().strip()
    candidates = {}
    for uid, info in directory.items():
        norm = re.sub(r"[^a-z0-9 ]", "", info["name"].lower())
        candidates[norm] = (uid, info["name"], info["state"])

    candidate_list = list(candidates.keys())
    results = []

    for scorer in [fuzz.ratio, fuzz.partial_ratio, fuzz.token_sort_ratio]:
        matches = process.extract(
            name_lower, candidate_list,
            scorer=scorer, score_cutoff=threshold, limit=10
        )
        for match_str, score, _ in matches:
            uid, real_name, state = candidates[match_str]
            if not any(r[0] == uid for r in results):
                results.append((uid, real_name, state, score))

    return sorted(results, key=lambda x: -x[3])


# ── Helpers ───────────────────────────────────────────────────────────────

def format_currency(val: float) -> str:
    if val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val/1_000:.0f}K"
    return f"${val:,.0f}"


def _render_footer():
    """Render the about/attribution footer."""
    meta = get_metadata()
    last_refreshed = meta.get("last_refreshed", "unknown")
    if last_refreshed != "unknown":
        try:
            dt = datetime.fromisoformat(last_refreshed)
            last_refreshed = dt.strftime("%B %d, %Y at %H:%M")
        except ValueError:
            pass

    st.markdown("---")
    st.markdown(
        f"""
**About PhD Finder**
This is an independent, open-source tool for exploring U.S. doctoral programs and
funded research. It is **not affiliated with** the National Science Foundation, the
National Center for Education Statistics, or any government agency.

**Data Sources**
- **Doctoral completions**: U.S. Department of Education, IPEDS Completions Survey
  (via the [scipeds](https://github.com/daniel-s-ford/scipeds) package)
- **Funded research**: NSF Award Search API
  (`research.gov/awardapi-service`)

**Known Limitations**
- IPEDS reports *completed* degrees, not currently open admissions programs.
  A program with zero recent graduates may still be active and admitting students.
- The NSF funding search only covers NSF-funded awards. Research funded by NIH,
  DARPA, DOE, industry, or foreign sources will not appear.
- A PI having an active award does not guarantee they are accepting new PhD students.
- Abstract keyword matching can produce false positives — read the abstract snippet
  before treating a match as confirmed-relevant.

**Last refreshed**: {last_refreshed}
"""
    )
    st.caption("Built by Jahanzeb — [learnwithjahanzeb.com](https://learnwithjahanzeb.com)")
    st.caption("Anonymous search queries may be logged to help improve this tool.")


def _render_admin_view():
    """Read-only admin dashboard showing usage stats from usage_log.csv."""
    st.markdown("---")
    st.header("📊 Admin — Usage Insights")

    if not USAGE_LOG.exists():
        st.info("No usage data logged yet.")
        return

    df = pd.read_csv(USAGE_LOG)
    if df.empty:
        st.info("Usage log is empty.")
        return

    st.metric("Total searches logged", len(df))

    # Searches by tab
    st.subheader("Searches by tab")
    tab_counts = df["tab"].value_counts()
    st.bar_chart(tab_counts)

    # Top field-search keywords
    field_df = df[df["tab"] == "field"]
    if not field_df.empty:
        st.subheader("Top field-search keywords (top 20)")
        kw_counts = field_df["query"].str.lower().value_counts().head(20)
        st.dataframe(kw_counts.reset_index().rename(columns={"query": "keyword", "count": "searches"}))

    # Top institution searches
    inst_df = df[df["tab"] == "institution"]
    if not inst_df.empty:
        st.subheader("Top institution searches (top 20)")
        inst_counts = inst_df["query"].str.lower().value_counts().head(20)
        st.dataframe(inst_counts.reset_index().rename(columns={"query": "institution", "count": "searches"}))

    # Dead-end searches (0 results)
    zero_df = df[df["num_results"] == 0]
    if not zero_df.empty:
        st.subheader(f"Searches with 0 results ({len(zero_df)} total)")
        zero_counts = zero_df.groupby(["tab", "query"]).size().reset_index(name="times")
        zero_counts = zero_counts.sort_values("times", ascending=False).head(30)
        st.dataframe(zero_counts)

    # Day-by-day volume
    st.subheader("Search volume by day")
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    daily = df.groupby("date").size()
    st.bar_chart(daily)


# ── Main UI ───────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="PhD Finder",
        page_icon="🎓",
        layout="wide",
    )

    # ── Google Analytics (GA4) ────────────────────────────────────────────
    st.markdown(
        f"""
        <script async src="https://www.googletagmanager.com/gtag/js?id={GA4_ID}"></script>
        <script>
          window.dataLayer = window.dataLayer || [];
          function gtag(){{ dataLayer.push(arguments); }}
          gtag('js', new Date());
          gtag('config', '{GA4_ID}');
        </script>
        """,
        unsafe_allow_html=True,
    )

    # ── Landing paragraph ─────────────────────────────────────────────────
    st.title("🎓 PhD Finder")
    st.markdown(
        "Search **U.S. doctoral programs** by field or institution, and explore "
        "**NSF-funded research awards** to find active professors and labs. "
        "Data comes from the U.S. Department of Education (IPEDS) and the "
        "National Science Foundation — two authoritative public sources. "
        "Enter a field like *chemistry*, *mechanical engineering*, or *sociology* "
        "to see which universities grant doctoral degrees in that area, or search "
        "by institution name to see all doctoral fields a school offers."
    )

    # ── Load data ─────────────────────────────────────────────────────────
    db = get_db()
    if db is None:
        st.error(
            f"IPEDS database not found at `{DB_PATH}`. "
            "Run `python refresh_cache.py` to build the data cache, "
            "or set the `IPEDS_DB_PATH` environment variable."
        )
        st.stop()

    cip_codes = get_all_cip_codes(db)
    directory = get_directory(db)

    # ── Sidebar ───────────────────────────────────────────────────────────
    st.sidebar.header("Settings")
    col1, col2 = st.sidebar.columns(2)
    year_start = col1.number_input("Start year", min_value=2000, max_value=2024, value=2020)
    year_end = col2.number_input("End year", min_value=2000, max_value=2024, value=2024)

    if year_start > year_end:
        st.sidebar.error("Start year must be <= end year")
        st.stop()

    st.sidebar.markdown(f"**Institutions:** {len(directory):,}")
    st.sidebar.markdown(f"**CIP codes:** {len(cip_codes):,}")

    meta = get_metadata()
    if meta.get("last_refreshed"):
        try:
            dt = datetime.fromisoformat(meta["last_refreshed"])
            st.sidebar.caption(f"Data refreshed: {dt:%b %d, %Y}")
        except ValueError:
            pass

    # ── Tabs ──────────────────────────────────────────────────────────────
    tab_field, tab_institution, tab_funded = st.tabs([
        "🔍 Search by Field",
        "🏛️ Search by Institution",
        "💰 Funded Research Search",
    ])

    # ─── Tab 1: Search by field/keyword ──────────────────────────────────
    with tab_field:
        st.subheader("Which universities grant doctoral degrees in this field?")

        keyword = st.text_input(
            "Enter a field name or keyword",
            placeholder="e.g. chemistry, mechanical engineering, sociology",
            max_chars=MAX_QUERY_LENGTH,
        )
        keyword = sanitize_input(keyword)

        fuzzy_threshold = st.slider(
            "Fuzzy match sensitivity", min_value=40, max_value=100, value=60,
            key="field_threshold",
            help="Lower = more results (broader matching), Higher = fewer results (stricter)"
        )

        if keyword:
            matches = fuzzy_match_cip(keyword, cip_codes, threshold=fuzzy_threshold)

            if not matches:
                st.warning(f"No CIP codes found matching '{keyword}' at threshold {fuzzy_threshold}")
            else:
                st.success(f"Found {len(matches)} matching CIP code(s)")

                with st.expander(f"Matched CIP codes ({len(matches)})", expanded=True):
                    for code, title, score in matches:
                        st.markdown(f"**{code}** — {title} *(score: {score:.0f})*")

                cip_prefixes = list(set(
                    code if len(code) <= 7 else code[:5]
                    for code, _, _ in matches
                ))

                results = query_doctoral_completions(db, cip_prefixes, year_start, year_end)

                if not results:
                    st.info(f"No doctoral completions found in {year_start}-{year_end} for these CIP codes")
                    _log_search("field", keyword, 0)
                else:
                    inst_data = {}
                    for unitid, cip, year, awards in results:
                        if unitid not in inst_data:
                            info = directory.get(unitid, {"name": "Unknown", "state": "?", "city": "?"})
                            inst_data[unitid] = {
                                "name": info["name"],
                                "state": info["state"],
                                "city": info["city"],
                                "total_phds": 0,
                                "cip_codes": {},
                                "by_year": {},
                            }
                        inst_data[unitid]["total_phds"] += awards
                        if cip not in inst_data[unitid]["cip_codes"]:
                            inst_data[unitid]["cip_codes"][cip] = 0
                        inst_data[unitid]["cip_codes"][cip] += awards
                        if year not in inst_data[unitid]["by_year"]:
                            inst_data[unitid]["by_year"][year] = 0
                        inst_data[unitid]["by_year"][year] += awards

                    sorted_insts = sorted(inst_data.items(), key=lambda x: -x[1]["total_phds"])

                    _log_search("field", keyword, len(sorted_insts))

                    states = sorted(set(info["state"] for info in inst_data.values()))
                    selected_states = st.multiselect("Filter by state", states, default=[], key="field_states")

                    if selected_states:
                        sorted_insts = [
                            (uid, info) for uid, info in sorted_insts
                            if info["state"] in selected_states
                        ]

                    st.markdown(f"### {len(sorted_insts)} institutions with doctoral completions "
                               f"({year_start}-{year_end})")

                    for rank, (unitid, info) in enumerate(sorted_insts, 1):
                        cip_summary = "; ".join(
                            f"{code} ({cip_codes.get(code, code)}): {count}"
                            for code, count in sorted(info["cip_codes"].items())
                        )
                        year_detail = ", ".join(
                            f"{y}: {c}" for y, c in sorted(info["by_year"].items())
                        )

                        with st.container():
                            cols = st.columns([1, 4, 1, 1])
                            cols[0].markdown(f"**{rank}**")
                            cols[1].markdown(f"**{info['name']}**  \n"
                                           f"_{info['city']}, {info['state']}_")
                            cols[2].markdown(f"**{info['total_phds']}** PhDs")
                            cols[3].caption(f"Years: {year_detail}")
                            st.caption(f"CIP: {cip_summary}")
                            st.divider()

                    # Export
                    if st.button("📥 Download results as CSV", key="field_export"):
                        output = io.StringIO()
                        output.write("Rank,Institution,State,City,Total PhDs,CIP Codes,Years\n")
                        for rank, (unitid, info) in enumerate(sorted_insts, 1):
                            cip_str = "; ".join(
                                f"{code} ({cip_codes.get(code, code)}): {count}"
                                for code, count in sorted(info["cip_codes"].items())
                            )
                            year_str = " ".join(
                                f"{y}:{c}" for y, c in sorted(info["by_year"].items())
                            )
                            output.write(
                                f'{rank},"{info["name"]}",{info["state"]},'
                                f'"{info["city"]}",{info["total_phds"]},'
                                f'"{cip_str}","{year_str}"\n'
                            )
                        st.download_button(
                            "Download CSV",
                            output.getvalue(),
                            file_name=f"phd_completions_{keyword.replace(' ', '_')}_{year_start}-{year_end}.csv",
                            mime="text/csv",
                        )

    # ─── Tab 2: Search by institution ────────────────────────────────────
    with tab_institution:
        st.subheader("What doctoral fields does this institution offer?")

        inst_name = st.text_input(
            "Enter an institution name",
            placeholder="e.g. MIT, Stanford, Georgia Tech",
            max_chars=MAX_QUERY_LENGTH,
            key="inst_input",
        )
        inst_name = sanitize_input(inst_name)

        inst_threshold = st.slider(
            "Match sensitivity", min_value=40, max_value=100, value=75,
            key="inst_threshold",
            help="Lower = more results (broader matching)"
        )

        if inst_name:
            matches = fuzzy_match_institution(inst_name, directory, threshold=inst_threshold)

            if not matches:
                st.warning(f"No institutions found matching '{inst_name}' at threshold {inst_threshold}")
                _log_search("institution", inst_name, 0)
            else:
                st.success(f"Found {len(matches)} matching institution(s)")

                _log_search("institution", inst_name, len(matches))

                for uid, real_name, state, score in matches[:10]:
                    with st.expander(f"{real_name} ({state}) — score: {score:.0f}", expanded=(len(matches) <= 3)):
                        results = query_institution_completions(db, uid, year_start, year_end)

                        if not results:
                            st.info(f"No doctoral completions found in {year_start}-{year_end}")
                        else:
                            cip_data = {}
                            for cip, year, awards in results:
                                if cip not in cip_data:
                                    cip_data[cip] = {"total": 0, "by_year": {}}
                                cip_data[cip]["total"] += awards
                                if year not in cip_data[cip]["by_year"]:
                                    cip_data[cip]["by_year"][year] = 0
                                cip_data[cip]["by_year"][year] += awards

                            total = sum(d["total"] for d in cip_data.values())
                            st.markdown(f"**Total doctoral completions ({year_start}-{year_end}): {total}**")

                            sorted_cips = sorted(cip_data.items(), key=lambda x: -x[1]["total"])

                            for cip, data in sorted_cips:
                                title = cip_codes.get(cip, "Unknown")
                                year_detail = ", ".join(
                                    f"{y}: {c}" for y, c in sorted(data["by_year"].items())
                                )
                                st.markdown(
                                    f"**{cip}** — {title}  \n"
                                    f"_{data['total']} completions_ | {year_detail}"
                                )

                            export_data = io.StringIO()
                            export_data.write("CIP Code,CIP Title,Total Completions,Years\n")
                            for cip, data in sorted_cips:
                                title = cip_codes.get(cip, "Unknown")
                                year_str = " ".join(
                                    f"{y}:{c}" for y, c in sorted(data["by_year"].items())
                                )
                                export_data.write(
                                    f'"{cip}","{title}",{data["total"]},"{year_str}"\n'
                                )
                            st.download_button(
                                "📥 Download this institution's data",
                                export_data.getvalue(),
                                file_name=f"phd_{real_name.replace(' ', '_')}_{year_start}-{year_end}.csv",
                                mime="text/csv",
                                key=f"export_{uid}",
                            )

    # ─── Tab 3: NSF Funded Research Search ────────────────────────────────
    with tab_funded:
        st.subheader("Search funded research awards")
        st.caption("Data from the NSF Award Search API, pre-loaded into a local cache. "
                   "Results are cross-referenced against the PhD verification database.")

        # Check if NSF cache exists
        if not NSF_CSV_PATH.exists():
            st.warning(
                "NSF award cache not found. Run `python refresh_cache.py` to build it."
            )
        else:
            nsf_df_full = pd.read_csv(NSF_CSV_PATH)

            # Keyword input
            kw_input = st.text_area(
                "Research keywords (comma-separated)",
                value="machine learning, deep learning, computer vision",
                height=80,
                max_chars=MAX_QUERY_LENGTH * MAX_KEYWORDS,
                help="Filter the cached NSF awards by these keywords.",
            )
            keywords = [sanitize_input(k.strip()) for k in kw_input.split(",") if k.strip()]
            keywords = keywords[:MAX_KEYWORDS]

            # Filter toggles
            col_f1, col_f2 = st.columns(2)
            active_only = col_f1.checkbox(
                "Active grants only",
                value=False,
                help="Only show awards with expiration date >= today",
            )
            lookback_years = col_f2.slider(
                "Expired grant lookback (years)",
                min_value=1, max_value=10, value=5,
                disabled=active_only,
            )

            if st.button("Search Awards", key="nsf_search_btn", type="primary"):
                if not keywords:
                    st.warning("Enter at least one keyword")
                else:
                    # Filter from pre-built cache (no API calls)
                    filtered = nsf_df_full.copy()

                    # Keyword filter — search across title, abstract, and matched keywords
                    if keywords:
                        def _kw_match(row):
                            text = f"{row.get('title','')} {row.get('abstract_snippet','')} {row.get('keyword_matched','')}".lower()
                            return any(k.lower() in text for k in keywords)
                        kw_mask = filtered.apply(_kw_match, axis=1)
                        filtered = filtered[kw_mask]

                    # Active filter
                    from datetime import timedelta
                    today = datetime.now().date()
                    if active_only:
                        filtered = filtered[filtered["is_active"] == True]
                    else:
                        cutoff = today - timedelta(days=lookback_years * 365)
                        filtered = filtered[
                            pd.to_datetime(filtered["exp_date"]).dt.date >= cutoff
                        ]

                    # Start year filter
                    filtered = filtered[
                        pd.to_datetime(filtered["start_date"]).dt.year >= year_start
                    ]

                    st.session_state["nsf_results"] = filtered
                    st.success(f"Found {len(filtered)} awards "
                               f"({filtered['pi_name'].nunique()} PIs, "
                               f"{filtered['institution'].nunique()} institutions)")

                    _log_search("nsf_funded", ", ".join(keywords), len(filtered))

            # Display results
            nsf_df = st.session_state.get("nsf_results")
            if nsf_df is not None and not nsf_df.empty:
                st.markdown("---")
                ff1, ff2, ff3 = st.columns(3)

                all_states = sorted(nsf_df["state"].dropna().unique())
                selected_states = ff1.multiselect("Filter by state", all_states, key="nsf_state")

                phd_col = "has_verified_phd" if "has_verified_phd" in nsf_df else "has_verified_cs_phd"
                phd_options = sorted(nsf_df[phd_col].dropna().unique())
                selected_phd = ff2.multiselect("PhD verification status", phd_options,
                                               default=phd_options, key="nsf_phd")

                all_kws = sorted(set(
                    kw for kws in nsf_df["keyword_matched"].dropna()
                    for kw in str(kws).split(", ")
                ))
                selected_kw = ff3.multiselect("Filter by keyword", all_kws, key="nsf_kw")

                # Apply filters
                display = nsf_df.copy()
                if selected_states:
                    display = display[display["state"].isin(selected_states)]
                if selected_phd:
                    display = display[display[phd_col].isin(selected_phd)]
                if selected_kw:
                    display = display[display["keyword_matched"].apply(
                        lambda x: any(k in str(x) for k in selected_kw)
                    )]

                sort_by = st.radio("Sort by", ["funds_obligated", "start_date", "institution"],
                                   horizontal=True, key="nsf_sort")
                display = display.sort_values(sort_by, ascending=(sort_by != "funds_obligated"))

                st.markdown(f"### {len(display)} awards "
                            f"({display['pi_name'].nunique()} PIs, "
                            f"{display['institution'].nunique()} institutions)")

                for _, row in display.iterrows():
                    phd_val = str(row.get(phd_col, "NotChecked"))
                    phd_badge = {"True": "✅ Verified PhD", "False": "❌ No PhD",
                                 "NotChecked": "⬜ Not checked"}.get(phd_val, "")
                    active_badge = "🟢 Active" if row["is_active"] else "⚪ Ended"

                    with st.container():
                        c1, c2 = st.columns([3, 1])
                        c1.markdown(
                            f"**{row['pi_name']}** — {row['institution']} ({row['state']})  \n"
                            f"_{row['title']}_"
                        )
                        c2.markdown(f"**{format_currency(row['funds_obligated'])}**  \n"
                                   f"{active_badge} | {phd_badge}")

                        meta_parts = [
                            f"Keywords: {row['keyword_matched']}",
                            f"Start: {row['start_date']}",
                            f"End: {row['exp_date']}",
                        ]
                        if row.get("co_pi_names"):
                            meta_parts.append(f"Co-PIs: {row['co_pi_names']}")
                        st.caption(" | ".join(meta_parts))

                        if row.get("abstract_snippet"):
                            with st.expander("Abstract snippet"):
                                st.write(row["abstract_snippet"])

                        st.divider()

                # Export buttons
                exp1, exp2 = st.columns(2)
                with exp1:
                    csv_data = display.to_csv(index=False)
                    st.download_button(
                        "📥 Download results as CSV",
                        csv_data,
                        file_name=f"nsf_awards_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv",
                        key="nsf_csv_export",
                    )
                with exp2:
                    fe = display.copy()
                    fe["professor_name"] = fe["pi_name"]
                    fe["research_area"] = fe["keyword_matched"]
                    fe["funding_status"] = fe.apply(
                        lambda r: f"Active (ends {r['exp_date']})" if r["is_active"]
                        else f"Ended {r['exp_date']}", axis=1,
                    )
                    fe["notes"] = fe.apply(
                        lambda r: f"{r['title']} | {format_currency(r['funds_obligated'])} | "
                        f"{r['start_date']} to {r['exp_date']}", axis=1,
                    )
                    faculty_csv = fe[["professor_name", "institution", "research_area",
                                       "funding_status", "notes"]].to_csv(index=False)
                    st.download_button(
                        "📋 Faculty Match Export (outreach tracker)",
                        faculty_csv,
                        file_name=f"faculty_targets_{datetime.now():%Y%m%d}.csv",
                        mime="text/csv",
                        key="nsf_faculty_export",
                    )

    # ── Footer ────────────────────────────────────────────────────────────
    _render_footer()

    # ── Admin view (hidden, accessible via ?admin=true) ───────────────────
    params = st.query_params
    if params.get("admin") == "true":
        _render_admin_view()


if __name__ == "__main__":
    main()
