"""Delete `.unavailable` markers that were written for a transient failure.

`cached_fetch` writes a `<name>.unavailable` marker the first time an endpoint fails,
and it is checked before every later run, so a marker is permanent for the life of the
cache. An earlier version wrote it for *any* failure, including rate limits and daily
quota exhaustion, which quietly retired working endpoints: a CoinGecko 429 blacklisted
coins, and an Nager.Date throttle blacklisted whole country-year holiday series.

A marker is only worth keeping when the endpoint gave a definitive refusal. This tool
removes the rest, so the next build retries them. It reads the reason recorded inside
each marker and never touches a marker that records a permanent status code.

    python tools/prune_markers.py            # report only
    python tools/prune_markers.py --apply    # delete the stale markers

Safe to run repeatedly, and safe to run on a cache with no manifest: it only ever
removes marker files, never a cache payload or a page.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.getenv("PSEO_CACHE_DIR") or ROOT / "cache")

# Statuses that mean the endpoint will still refuse tomorrow. Anything else - a 429, a
# daily quota, a DNS blip, a timeout - was transient and its marker is stale.
PERMANENT_CODES = ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 410", "HTTP 451")


def is_stale(reason: str) -> bool:
    return not any(code in reason for code in PERMANENT_CODES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete the stale markers")
    ap.add_argument("--cache", default=str(CACHE))
    args = ap.parse_args()

    cache = Path(args.cache)
    if not cache.is_dir():
        print(f"no cache directory at {cache}; nothing to prune")
        return 0

    markers = sorted(cache.glob("*.unavailable"))
    if not markers:
        print(f"no .unavailable markers in {cache}")
        return 0

    stale: list[Path] = []
    for marker in markers:
        try:
            reason = marker.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if is_stale(reason):
            stale.append(marker)

    keep = len(markers) - len(stale)
    print(f"{len(markers)} markers in {cache}: {len(stale)} stale, {keep} permanent")
    for marker in stale:
        print(f"  stale  {marker.name}")
    if not args.apply:
        print("\nplan only, nothing deleted. re-run with --apply to delete them")
        return 0

    for marker in stale:
        try:
            marker.unlink()
        except OSError as exc:
            print(f"  could not delete {marker.name}: {exc}")
            return 1
    print(f"deleted {len(stale)} stale markers; those endpoints are retried on the next build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
