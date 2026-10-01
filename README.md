# Autonomous pSEO Engine

Zero-competition, long-tail data pages generated on a schedule, published to `gh-pages`
(or Cloudflare Pages) and monetised with display ads plus a CPA fallback. Everything runs
on free tiers.

```
scrape (public APIs) -> Gemini enrichment (cached) -> unique HTML -> delta write
      -> sitemap/robots/hubs -> IndexNow ping -> gh-pages
```

## What gets published

| Source | Endpoint | Key needed | Pages |
| --- | --- | --- | --- |
| Public holidays | `date.nager.at` | no | `public-holidays-germany-2026`, plus individual holiday pages |
| Country data | `api.worldbank.org` | no | `belgium-country-data`, `population-of-belgium`, `most-populated-countries` |
| City climate | `power.larc.nasa.gov` (NASA POWER) | no | `berlin-average-monthly-temperature-rainfall` |
| Crypto | `api.coingecko.com` (365-day window) | no | `bitcoin-price-by-month` |

Every page has a unique title/meta/canonical, a key-figures grid, a real data table,
JSON-LD (`WebPage` + `Dataset` or `Event` + `BreadcrumbList`), internal links to 12 sibling
pages plus its category hub, and three ad slots with a JS waterfall fallback.

## Layout

```
generator.py              CLI entrypoint
config.json               site, limits, sources, ads, indexing
engine/config.py          config loading, paths, secret reader
engine/http.py            aiohttp client: retries, backoff, pacing, JSON only
engine/ratelimit.py       Gemini rate limiter, token bucket, per-run budget
engine/models.py          Page / Link / slugify / content hash
engine/enrich.py          Gemini client + cache + deterministic fallback copy
engine/render.py          theme/CSS, blocks, JSON-LD, ad waterfall
engine/hubs.py            internal linking, hub pages, sitemap, robots
engine/indexing.py        IndexNow + optional Google Indexing API
engine/writer.py          manifest, delta writes, orphan cleanup
engine/sources/*.py       one module per data source
assets/cities.json        130 cities with coordinates
assets/countries.json     58 countries for holiday coverage
cache/                    raw API snapshots, Gemini cache, manifest (git-ignored)
output/                   published HTML, sitemap.xml, robots.txt
.github/workflows/deploy.yml   daily cron build + gh-pages deploy
tests/test_engine.py      21 offline unit tests
```

## Local run

```bash
pip install -r requirements.txt
python generator.py --dry-run --limit 40     # collect + render, no search-engine pings
python -m unittest discover -s tests          # offline tests, no network
python -m http.server -d output 8080         # preview
```

Flags: `--limit N` (cap pages), `--no-gemini` (deterministic copy), `--dry-run` (no pings),
`--prune` (drop manifest entries with no file), `--config path.json`, `--log-level DEBUG`.

Without a Gemini key the engine still publishes: `engine/enrich.py` falls back to
deterministic copy built from the page's own facts, so no page is ever empty.

## Secrets to add once the code is wired up

GitHub -> repository -> Settings -> Secrets and variables -> Actions:

| Secret | Purpose | Required |
| --- | --- | --- |
| `GEMINI_API_KEY` | AI Studio key for copy enrichment | optional (fallback copy otherwise) |
| `INDEXNOW_KEY` | random 32-char hex string; also written to `output/<key>.txt` | recommended |
| `GOOGLE_INDEXING_CREDENTIALS` | service-account JSON | not recommended, see below |

Then set `config.json -> domain` to the published URL (Cloudflare Pages or
`https://<user>.github.io/<repo>/`) so canonicals, sitemap and IndexNow host match.

## How the cost controls work

- **Gemini pacing**: `RateLimiter` spaces calls at least 4.1s apart and also enforces a
  rolling 15-per-minute window, with 8s/16s/24s backoff on failure.
- **Per-run budget**: `max_gemini_calls_per_run` caps spend; the cache is keyed by data
  key, so a re-run of unchanged data costs zero calls.
- **Delta writes**: `cache/manifest.json` stores a content hash per page; unchanged pages
  are not rewritten and not re-notified.
- **Unseen-first**: each run orders pages so new slugs are generated before already
  published ones, so a limited run still grows the corpus instead of looping on the same head.
- **Dead endpoints**: a source that fails with no cached copy writes an `.unavailable`
  marker and is skipped on later runs instead of burning retries.
- **CI minutes**: one daily run, `concurrency` guard, `keep_files: true` so published pages
  accumulate, caches restored with a rolling key.

## Ad monetisation

Put your network tag into `config.json -> monetization`:

```json
"adsterra_script": "<script async src=...></script>",
"monetag_script": "<script>(...)</script>",
"cpa_fallback_url": "https://your-affiliate-link/offer",
"cpa_fallback_image": "https://your-creative.example/728x90.png"
```

Three slots (`top`, `mid`, `foot`) render the network tag. `engine/render.py`'s waterfall
script checks each slot after 2.5s (and again at 6s): if the slot is under 40px tall,
under 120px wide, or contains no text, it is replaced once with the CPA creative
(`rel="nofollow sponsored noopener"`). Reserve fixed slot heights in the network's dashboard
to avoid layout shift.

## Known caveats, stated plainly

1. **Google Indexing API is off by default.** Google restricts it to `JobPosting` and
   `BroadcastEvent`; submitting ordinary content pages through it risks a manual action.
   The code path exists and works, but use IndexNow plus the sitemap instead. If you do
   enable it, set `indexing.google_indexing_api.enabled = true`.
2. **Randomised CSS class names are not an SEO trick.** They reduce identical-template
   fingerprints but Google does not reward them, and cloaking-style variation can hurt.
   Keep `seo.randomize_dom = true` for mild variation, not as a ranking lever.
3. **Crypto history is capped at 365 days** on the key-free CoinGecko tier, so those pages
   are rolling 12-month summaries, not all-time history.
4. **Zero-competition does not mean traffic.** Ranking still needs authority, indexing and
   a few hundred indexed pages. The engine produces supply cheaply; demand is your problem.
5. **Adsterra/Monetag approval** requires real traffic on an established domain. Expect
   rejection on a brand-new Pages subdomain; a cheap domain plus a few weeks of indexed
   pages is the usual path.

## Adding a data source

1. Create `engine/sources/<name>.py` with `NAME`, `async def collect(cfg, http, budget) -> list[Page]`.
2. Fetch through `base.cached_fetch(...)` so failures fall back to the cached snapshot and
   permanently dead endpoints get an `.unavailable` marker.
3. Return `Page` objects with `data["key"]` (drives the Gemini cache), `facts`, and
   optionally `data["table"]` / `data["sections"]`.
4. Register it in `engine/sources/__init__.py` `MODULES` and add its kind to
   `SECTIONS` in `engine/hubs.py` so it gets a hub page and related links.
5. Add a fixture-driven unit test in `tests/test_engine.py`.