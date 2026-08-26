#!/usr/bin/env python3
"""
Download the scipeds IPEDS database into the container's data directory.
Called during `docker build` so the app starts with data ready.
"""

from pathlib import Path
from scipeds import download_db

OUTPUT = Path("/app/data/scipeds.duckdb")
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

print(f"Downloading scipeds database to {OUTPUT} ...")
download_db(output_path=OUTPUT, overwrite=False, verbose=True)
print(f"Done. Size: {OUTPUT.stat().st_size / 1e6:.0f} MB")
