# PhD Finder — Docker deployment

FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies (includes scipeds for build-time DB download)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Download IPEDS database at build time ────────────────────────────────
# This runs during `docker build`, not at startup. The 317MB download
# is cached by Docker's layer cache — it only re-runs if requirements.txt
# changes (i.e. scipeds version bumps).
COPY build_cache.py .
RUN python build_cache.py

# Copy application code (changes frequently — last layer for best caching)
COPY . .

# ── Environment ───────────────────────────────────────────────────────────
# IPEDS_DB_PATH points at the pre-downloaded database inside the container.
# Override via env var if you mount a different DB file.
ENV IPEDS_DB_PATH=/app/data/scipeds.duckdb
ENV PYTHONUNBUFFERED=1

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "phd_search_app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
