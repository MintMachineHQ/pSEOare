"""One-off and recurring URL submission to Bing Webmaster Tools.

IndexNow already tells Bing about every page the engine creates, so this is not
needed for day-to-day operation. It exists to close the gap for pages published
before IndexNow was working, and to force a re-crawl of the whole corpus on
demand.

The key lives outside the repository in ~/.secrets/bing-webmaster-key, mode 600.

    python3 tools/bing_submit.py                  # plan only, nothing is sent
    python3 tools/bing_submit.py --submit         # submit everything in the sitemap
    python3 tools/bing_submit.py --submit --limit 100
    python3 tools/bing_submit.py --quota          # ask Bing what is left today
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://ssl.bing.com/webmaster/api.svc/json"
# Overridable so CI can mount the key from a secret without writing to $HOME.
KEY_PATH = os.path.expanduser(
    os.getenv("BING_KEY_PATH") or "~/.secrets/bing-webmaster-key"
)
SITEMAP = "https://pseoare.pages.dev/sitemap.xml"
# Bing rejects a batch larger than 100 URLs with HTTP 400 / ErrorCode 8, and the
# per-domain allowance is small too (100/day, 2,900/month as reported by
# GetUrlSubmissionQuota). Both numbers are read from the quota endpoint at run
# time; these are only fallbacks when that call fails.
BATCH = 100
FALLBACK_DAILY_QUOTA = 100


def key() -> str:
    try:
        value = open(KEY_PATH).read().strip()
    except FileNotFoundError:
        sys.exit(f"no key at {KEY_PATH}")
    if not value:
        sys.exit(f"{KEY_PATH} is empty")
    return value


def call(method: str, api_key: str, payload: dict | None = None, get: bool = False) -> dict:
    """Call a Bing Webmaster API method.

    SubmitUrlbatch takes a JSON body on POST. GetUrlSubmissionQuota is a GET and takes
    its arguments in the query string; posting to it answers 405.
    """
    params = {"apikey": api_key}
    body = None
    if get:
        params.update(payload or {})
    elif payload is not None:
        body = json.dumps(payload).encode()
    url = f"{API}/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=body,
        method="GET" if get else "POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "pseoare"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body_text = resp.read().decode().strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        hint = {
            401: "the key was rejected. Generate a fresh one in Bing Webmaster Tools",
            403: "the key is valid but not permitted for this site",
            405: "wrong HTTP method or endpoint name for this method",
            429: "Bing is rate limiting; try again later",
        }.get(exc.code, "")
        sys.exit(f"{method} failed: HTTP {exc.code} {hint}\n{detail}")
    if not body_text:
        return {}
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return {"raw": body_text[:300]}


def sitemap_urls(source: str) -> list[str]:
    req = urllib.request.Request(source, headers={"User-Agent": "pseoare"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            xml = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        sys.exit(f"could not read {source}: HTTP {exc.code}")
    urls = re.findall(r"<loc>(.*?)</loc>", xml)
    if not urls:
        sys.exit(f"no <loc> entries in {source}")
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def quota(api_key: str, site: str, quiet: bool = False) -> int:
    result = call("GetUrlSubmissionQuota", api_key, {"siteUrl": site}, get=True)
    data = result.get("d", result) if isinstance(result, dict) else {}
    daily = int(data.get("DailyQuota") or FALLBACK_DAILY_QUOTA)
    monthly = data.get("MonthlyQuota")
    if not quiet:
        print(json.dumps(result, indent=2)[:800])
    return daily


def prioritise(urls: list[str]) -> list[str]:
    """Order the sitemap so long-tail pages are submitted before the hubs.

    The allowance is 100 URLs/day against a corpus of thousands, so the order
    matters: the hub indexes and the homepage are already discoverable from any
    single deep link, while the individual pages are the ones that can win a
    query. Sending hubs first would spend the budget on the pages that need it
    least.
    """
    def rank(url: str) -> int:
        # Compare the path, not the last "/"-separated chunk of the whole URL: the
        # homepage has no path component at all, so rsplit on the raw string leaves
        # the hostname behind and quietly ranks it as a deep page.
        path = urllib.parse.urlparse(url).path.strip("/")
        return 1 if (not path or path.startswith("hub-")) else 0

    return sorted(urls, key=rank)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="actually send (default: plan only)")
    ap.add_argument("--limit", type=int, default=0, help="submit at most N urls")
    ap.add_argument("--site", default="https://pseoare.pages.dev")
    ap.add_argument("--sitemap", default=SITEMAP)
    ap.add_argument("--quota", action="store_true", help="print the daily quota and exit")
    args = ap.parse_args()

    api_key = key()
    if args.quota:
        quota(api_key, args.site)
        return

    daily = quota(api_key, args.site, quiet=True)

    urls = prioritise(sitemap_urls(args.sitemap))
    total = len(urls)

    # Never send more than the day's remaining allowance: Bing counts the whole
    # submitted batch against the quota and answers HTTP 400 if it overflows,
    # discarding the batch rather than partially accepting it.
    sendable = min(total, daily)
    if args.limit:
        sendable = min(sendable, args.limit)

    print(f"site     {args.site}")
    print(f"sitemap  {args.sitemap}")
    print(f"urls     {total} unique in the sitemap")
    print(f"quota    {daily} left today -> sending at most {sendable}")
    if sendable < total:
        print(f"NOTE     {total - sendable} urls wait for a later day; the sitemap and")
        print("         IndexNow still cover them, so nothing is left undiscovered.")

    urls = urls[:sendable]
    if not urls:
        print("nothing to submit today")
        return
    batches = [urls[i : i + BATCH] for i in range(0, len(urls), BATCH)]

    if not args.submit:
        print("\nplan only, nothing sent. re-run with --submit to send")
        print("first 3:", *urls[:3], sep="\n  ")
        return

    sent = 0
    for index, batch in enumerate(batches, 1):
        call("SubmitUrlbatch", api_key, {"siteUrl": args.site, "urlList": batch})
        sent += len(batch)
        print(f"  batch {index}/{len(batches)} accepted ({len(batch)} urls)")
    print(f"submitted {sent} urls to Bing")


if __name__ == "__main__":
    main()
