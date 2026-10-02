"""Pull Adsterra publisher stats and print a revenue summary.

Read-only. The publisher API is GET-only, so this can never change account
settings. The token lives outside the repo in ~/.secrets/adsterra-token.

    python3 tools/adsterra_stats.py            # last 7 days
    python3 tools/adsterra_stats.py 30         # last 30 days
    python3 tools/adsterra_stats.py 7 --json   # machine-readable
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api3.adsterratools.com/publisher"
TOKEN_PATH = os.path.expanduser("~/.secrets/adsterra-token")
DAILY_TARGET_USD = 40.0


def token() -> str:
    try:
        value = open(TOKEN_PATH).read().strip()
    except FileNotFoundError:
        sys.exit(f"no token at {TOKEN_PATH}. Create one in Adsterra: Settings -> API.")
    if not value:
        sys.exit(f"{TOKEN_PATH} is empty")
    return value


def get(path: str, key: str, **params) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    url = f"{API}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        hint = {
            401: "token is incorrect",
            403: "token is no longer valid, generate a new one",
            404: "bad endpoint",
        }.get(exc.code, "")
        sys.exit(f"HTTP {exc.code} {hint}: {detail}")
    except Exception as exc:  # network, DNS, bad JSON
        sys.exit(f"request failed: {exc!r}")


def collect(days: int) -> dict:
    key = token()
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    domains = get("domains.json", key).get("items", [])
    placements = get("placements.json", key).get("items", [])

    series = []
    for domain in domains:
        rows = get(
            "stats.json",
            key,
            domain=domain["id"],
            start_date=start,
            finish_date=end,
            **{"group_by[]": "date"},
        ).get("items", [])
        for row in rows:
            row["domain"] = domain["title"]
            series.append(row)

    by_placement = get(
        "stats.json", key, start_date=start, finish_date=end, **{"group_by[]": "placement"}
    ).get("items", [])

    names = {p["id"]: f"{p.get('alias') or p.get('title') or p['id']} ({p['id']})" for p in placements}
    return {
        "range": [str(start), str(end)],
        "days": days,
        "domains": [{"id": d["id"], "title": d["title"]} for d in domains],
        "placements": [
            {"id": p["id"], "title": p["title"], "domain_id": p["domain_id"]} for p in placements
        ],
        "daily": series,
        "by_placement": [
            {
                "placement": names.get(r["placement"], str(r["placement"])),
                "placement_id": r["placement"],
                "impression": r.get("impression", 0),
                "clicks": r.get("clicks", 0),
                "ctr": r.get("ctr", 0),
                "cpm": r.get("cpm", 0),
                "revenue": r.get("revenue", 0),
            }
            for r in by_placement
        ],
    }


def report(data: dict) -> None:
    days = max(data["days"], 1)
    print(f"Adsterra  {data['range'][0]} -> {data['range'][1]}  ({days}d)")
    print("domains  " + ", ".join(f"{d['title']}#{d['id']}" for d in data["domains"]))

    totals = {"impression": 0, "clicks": 0, "revenue": 0.0}
    for row in data["daily"]:
        totals["impression"] += row.get("impression", 0)
        totals["clicks"] += row.get("clicks", 0)
        totals["revenue"] += float(row.get("revenue", 0) or 0)

    print("\nper placement")
    if not data["by_placement"]:
        print("  (no data yet)")
    for row in data["by_placement"]:
        print(
            f"  {row['placement']:<28} impressions={row['impression']:<8}"
            f" clicks={row['clicks']:<6} ctr={row['ctr']:<6} revenue=${row['revenue']:.4f}"
        )

    per_day_rev = totals["revenue"] / days
    active_days = len({r["date"] for r in data["daily"] if r.get("impression", 0)})
    print(
        f"\ntotals    impressions={totals['impression']} clicks={totals['clicks']} "
        f"revenue=${totals['revenue']:.4f}"
    )
    print(
        f"average   {totals['impression'] / days:.1f} impressions/day, "
        f"${per_day_rev:.4f}/day over {days} days ({active_days} with any impression)"
    )
    if per_day_rev > 0:
        print(f"target    ${DAILY_TARGET_USD:.0f}/day -> {per_day_rev / DAILY_TARGET_USD * 100:.1f}% of goal")
    else:
        print(f"target    ${DAILY_TARGET_USD:.0f}/day -> no revenue recorded in this window")

    silent = [
        p["title"]
        for p in data["placements"]
        if not any(
            r["placement_id"] == p["id"] and r["impression"] for r in data["by_placement"]
        )
    ]
    if silent:
        print("no impressions  " + ", ".join(silent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="?", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = collect(max(args.days, 1))
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        report(data)


if __name__ == "__main__":
    main()
