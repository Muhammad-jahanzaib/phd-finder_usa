# CS PhD Verification - Methodology

## Data Source
- U.S. Department of Education IPEDS (Integrated Postsecondary Education Data System)
- Accessed via the `scipeds` Python package (pre-processed DuckDB database)
- Source: NCES IPEDS Completions survey (C_A component)

## CIP Codes Checked (CS Family)
- **11.xxxx**: Computer and Information Sciences (all subcodes)
  - 11.0101: Computer and Information Sciences, General
  - 11.0701: Computer Science
  - 11.0401: Information Science/Studies
  - And other 11.x subcodes
- **14.09xx**: Computer Engineering
  - 14.0901: Computer Engineering

## Award Level
- IPEDS award level: "Doctor's degree - research/scholarship"
- Filtered to MAJORNUM=1 (first major only) to avoid double-counting

## Lookback Window
- Years 2020-2024 (5 years of completions data)

## Method
1. Loaded full IPEDS institution directory from scipeds database
2. Queried all schools with doctoral completions in CS-family CIP codes (2020-2024)
3. Fuzzy-matched input university names against IPEDS official names (rapidfuzz)
4. For each matched school, checked if ANY doctoral CS completions exist in window

## Matching
- Threshold: 85 (rapidfuzz scoring: ratio, partial_ratio, token_sort_ratio)
- Manual aliases for known name variations (e.g. 'The Johns Hopkins University')
- Scores below threshold flagged as NEEDS_REVIEW

## Rename-Pattern Second Pass
- Applied only to NEEDS_REVIEW entries from the first pass
- Tests common historical rename patterns:
  - College -> University, State College -> State University
  - Normal School -> University, Seminary -> University
  - Strip trailing 'of <Place>' qualifiers
  - Normalize ampersands, hyphens, commas
- Pattern-matched entries labeled CONFIRMED_VIA_RENAME / NO_PHD_VIA_RENAME
- Resolved 96 of 252 NEEDS_REVIEW entries

## Web-Resolution Third Pass
- Applied to remaining NEEDS_REVIEW entries after rename-pattern pass
- Web search used to find current official names of renamed institutions
- Resolved names re-run through fuzzy_match_school + rename-pattern logic
- Pattern-matched entries labeled CONFIRMED_VIA_WEB_RESOLVE / NO_PHD_VIA_WEB_RESOLVE
- Full resolution chain logged for auditability

## Caveats
- IPEDS reports completions (graduations), not currently-open admissions
- A program with zero recent graduates may still be new/admitting
- Programs established very recently may not appear in the lookback window
- Some schools may report CS PhDs under CIP codes outside the checked range
- Small programs (fewer than ~3 graduates in 5 years) may be suppressed by IPEDS
- IPEDS uses institutional names that may differ from common usage

## Summary
- CONFIRMED (direct): 212 schools have IPEDS-documented CS doctoral completions
- CONFIRMED_VIA_RENAME: 5 matched via rename-pattern second pass
- CONFIRMED_VIA_WEB_RESOLVE: 0 matched via web-resolved names
- NO_PHD_PROGRAM (direct): 1340 matched but no CS doctoral completions
- NO_PHD_VIA_RENAME: 91 matched via rename, no CS doctoral completions
- NO_PHD_VIA_WEB_RESOLVE: 22 matched via web-resolved names, no CS doctoral completions
- NEEDS_REVIEW: 134 could not be confidently matched

---

## NSF Award Search (Supplementary Data Source)

### Overview
The NSF Award Search API provides information on NSF-funded research awards, complementing the IPEDS-based "does a PhD program exist" answer with "who is currently funded to do this research." This surfaces specific professors, labs, and research areas rather than just institutions.

### Data Source
- NSF Award Search API (free, no API key required)
- Endpoint: `https://www.research.gov/awardapi-service/v1/awards.json`
- Fields queried: id, title, piFirstName, piLastName, coPDPI, awardeeName, awardeeStateCode, startDate, expDate, fundsObligatedAmt, abstractText

### Implementation (`nsf_search.py`)
- Queries the NSF API once per keyword with polite rate limiting (1s delay between calls)
- Caches raw JSON responses locally (7-day expiry) to avoid redundant API calls
- Parses results into a flat, deduplicated table (awards matching multiple keywords are merged)
- Cross-references institutions against `cs_phd_verification.csv` using the same fuzzy matching threshold (85) as the main verification pipeline

### Streamlit Integration (Tab 3: "Funded Research Search")
- Multi-keyword input with configurable defaults
- Toggle between active-only grants and configurable expired lookback (1-10 years)
- Filterable by state, CS PhD verification status, and matched keyword
- Sortable by funding amount, start date, or institution
- CSV export and Faculty Match Export (outreach tracker format)

### Caveats (NSF Award Search)
- **NSF-only lens**: This data source only covers NSF-funded work. NIH-funded research (very common for Alzheimer's, clinical neuroimaging, and biomedical AI), DARPA, DOE, industry-funded, and foreign-funded grants (e.g. UK/Germany/EU sources for international targets like Oxford, Bonn) will NOT appear. The absence of a PI here does not mean they lack funding.
- **Funding ≠ student availability**: A PI having an active award does not guarantee they are currently accepting new PhD students. Award activity is a proxy signal for active research, not a confirmation of open positions.
- **Abstract keyword false positives**: Keyword matching against `abstractText` can produce false positives when a keyword appears in an unrelated context within the abstract (e.g., "federated learning" mentioned in a broader AI infrastructure grant). Users should read the abstract snippet before treating any match as confirmed-relevant.
- **US institutions only**: The NSF API covers US-based awardee institutions. International collaborators may appear as co-PIs but the award is tied to the US institution.
- **Rate limits**: The API has soft rate limits. The module caches responses locally (7-day expiry) to minimize repeated calls. Bulk searches across many keywords will be slower due to polite 1-second delays between requests.

---

## Usage Logging & Analytics

### Page-View Analytics
Google Analytics 4 (GA4) is injected via a script tag in the Streamlit app's HTML head.
Measurement ID: `G-V1BCYS79RM`.

### Anonymous Search Logging
Every search performed (field search, institution search, and NSF funded research search)
is logged to `data/usage_log.csv` with the following fields only:
- `timestamp` (ISO format, second precision)
- `tab` (which search tab was used)
- `query` (the search text)
- `num_results` (number of results returned, 0 if none)

**No IP addresses, session IDs, user agents, or other identifying information is recorded.**

### Admin Insights
A read-only admin dashboard is available at `?admin=true` (not linked in the public UI).
It reads `usage_log.csv` and shows: total searches, top keywords, top institutions,
zero-result searches, and daily volume.

### Data Persistence Limitation
The usage log (`data/usage_log.csv`) is stored inside the Docker container's filesystem.
On platforms with ephemeral storage (e.g. Render free tier, Railway, Fly.io),
**this log will be lost on every redeploy or restart**. To preserve data long-term,
you would need to either:
1. Mount a persistent disk at `/app/data/`
2. Ship logs to an external service (e.g. a database, S3, or a logging API)
3. Run the app on a server with a persistent filesystem

This limitation is noted honestly rather than silently losing data.
