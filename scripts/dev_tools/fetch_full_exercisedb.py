#!/usr/bin/env python3
"""One-off bulk import of the full ExerciseDB catalog into a browsable video
library (separate from app/activity_taxonomy.csv, which only covers the
exercises our plans actually prescribe).

This key's RapidAPI plan has a 690 request/month hard cap. The exercise list
itself costs 1 request; every GIF download costs 1 more. So we fetch the full
metadata list for free, then download GIFs for as many exercises as the
remaining quota allows (leaving a small safety buffer) and record which ones
were skipped so a future run (next month's quota, or an upgraded plan) can
pick up where this left off.

Usage:
    RAPIDAPI_KEY=... python scripts/dev_tools/fetch_full_exercisedb.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
GIF_DIR = ROOT / "app" / "static" / "exercisedb_gifs"
CATALOG_PATH = DATA / "exercisedb_catalog.json"
PROGRESS_PATH = DATA / "exercisedb_fetch_progress.json"

API = "https://exercisedb.p.rapidapi.com"
KEY = os.environ.get("RAPIDAPI_KEY")
if not KEY:
    sys.exit("Set RAPIDAPI_KEY in the environment first.")

HEADERS = {
    "X-RapidAPI-Key": KEY,
    "X-RapidAPI-Host": "exercisedb.p.rapidapi.com",
    # Cloudflare (error 1010) blocks urllib's default User-Agent on this host.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
SAFETY_BUFFER = 10  # leave headroom instead of running the quota to exactly 0


def _get(path: str, timeout: int = 20):
    req = urllib.request.Request(f"{API}{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        remaining = resp.headers.get("X-RateLimit-Requests-Remaining")
        return json.loads(resp.read().decode()), remaining


PAGE_SIZE = 10  # this RapidAPI deployment ignores a bigger ?limit and always
                 # returns 10 items per page (confirmed by testing) -- offset
                 # is the only way to page through the full catalog.


def fetch_catalog() -> tuple[list[dict], int | None]:
    if CATALOG_PATH.exists():
        cached = json.loads(CATALOG_PATH.read_text())
        print(f"Catalog already cached at {CATALOG_PATH} ({len(cached)} exercises), reusing it.")
        return cached, None
    print("Fetching full exercise list (paginated, 10/request)...")
    all_exercises: list[dict] = []
    offset = 0
    remaining = None
    while True:
        page, remaining = _get(f"/exercises?limit={PAGE_SIZE}&offset={offset}")
        if not page:
            break
        all_exercises.extend(page)
        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE:
            break
        if offset % 200 == 0:
            print(f"  ...{offset} so far, requests remaining: {remaining}")
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(all_exercises, indent=2))
    print(f"Got {len(all_exercises)} exercises total. Requests remaining: {remaining}")
    return all_exercises, remaining


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text())
    return {"fetched_ids": [], "failed_ids": []}


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2))


def main() -> None:
    catalog, remaining_after_catalog = fetch_catalog()
    progress = load_progress()
    fetched = set(progress["fetched_ids"])
    GIF_DIR.mkdir(parents=True, exist_ok=True)

    if remaining_after_catalog is None:
        # Catalog was cached from a previous run -- ask for current quota without
        # wasting a request on a throwaway page we'd discard.
        _, remaining_header = _get(f"/exercises?limit={PAGE_SIZE}&offset=0")
        remaining_after_catalog = int(remaining_header) if remaining_header else 0
    budget = max(0, int(remaining_after_catalog) - SAFETY_BUFFER)
    print(f"Requests remaining: {remaining_after_catalog}. Budget for GIFs this run: {budget}")

    todo = [e for e in catalog if e["id"] not in fetched]
    got = 0
    for ex in todo:
        if got >= budget:
            print(f"Quota budget reached after {got} GIFs. Stopping cleanly.")
            break
        eid = ex["id"]
        out = GIF_DIR / f"{eid}.gif"
        try:
            req = urllib.request.Request(
                f"{API}/image?exerciseId={eid}&resolution=180", headers=HEADERS
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                out.write_bytes(resp.read())
            progress["fetched_ids"].append(eid)
            got += 1
            if got % 25 == 0:
                print(f"  {got}/{budget} downloaded...")
                save_progress(progress)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"  FAILED {eid} ({ex['name']}): {exc}")
            progress["failed_ids"].append(eid)
        time.sleep(0.05)

    save_progress(progress)
    remaining_total = len(catalog) - len(set(progress["fetched_ids"]))
    print(
        f"\nDone. {len(progress['fetched_ids'])} GIFs saved to {GIF_DIR}. "
        f"{remaining_total} exercises still without a GIF (quota or failure) -- "
        f"re-run next billing cycle to continue, progress is saved in {PROGRESS_PATH}."
    )


if __name__ == "__main__":
    main()
