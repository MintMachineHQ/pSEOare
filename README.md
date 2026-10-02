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

`seo.url_style` controls link shape: `clean` for Cloudflare Pages (no `.html`, which is what
the site uses), `html` if you ever move back to GitHub Pages. Getting this wrong makes every
canonical point at a redirect, which is the difference between being indexed and ignored.

Without a Gemini key the engine still publishes: `engine/enrich.py` falls back to
deterministic copy built from the page's own facts, so no page is ever empty.

## Secrets to add once the code is wired up

GitHub -> repository -> Settings -> Secrets and variables -> Actions:

| Secret | Purpose | Required |
| --- | --- | --- |
| `GEMINI_API_KEY` | AI Studio key for copy enrichment | optional (fallback copy otherwise) |
| `INDEXNOW_KEY` | 32-char hex string; also written to `output/<key>.txt` | recommended |
| `GOOGLE_INDEXING_CREDENTIALS` | service-account JSON | not recommended, see below |

Then set `config.json -> domain` to the published URL (Cloudflare Pages or
`https://<user>.github.io/<repo>/`) so canonicals, sitemap and IndexNow host match.

## How the cost controls work

- **Gemini pacing**: `RateLimiter` spaces calls `gemini_min_interval_seconds` apart (7.5s by
  default) and enforces a rolling per-minute window. On `429` it honours `Retry-After`, halves
  the pace after two rate limits, and never counts a rate limit as a broken key. A hard
  rejection (400/401/403/404) disables enrichment for the rest of the run instead of retrying
  every page, so a bad key costs one run rather than twenty minutes.
- **Model discovery**: the model is never hardcoded. Each run asks
  `v1beta/models` which models the key can call, drops non-text endpoints (TTS, image,
  transcription), prefers the newest stable flash model, and falls back through a candidate
  list if the chosen one 404s. `gemini-1.5-flash` already returns 404.
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

## Publishing targets

| Target | How | When |
| --- | --- | --- |
| Build artifact | `deploy.yml`, daily cron, publishes to `gh-pages` | automatic |
| **Public site** | `deploy-cloudflare.yml` mirrors `gh-pages` to Cloudflare Pages | automatic, runs right after the build |

The public site is **https://pseoare.pages.dev**. The `gh-pages` branch is the build
artifact the mirror reads; it is not advertised, because the engine emits Cloudflare clean
URLs (no `.html`) and those would not resolve on GitHub Pages.

**Why Cloudflare matters:** IndexNow validates its key at the domain root, so
`https://user.github.io/repo/` fails with `403 UserForbiddedToAccessSite` and pings are
skipped. `https://<project>.pages.dev` is a root domain, so switching the domain turns
IndexNow on with no code change. Cloudflare also gives you Bot Fight Mode for scraper
defenders.

Setup, about ten minutes of clicking:

1. Cloudflare dashboard -> **Workers & Pages** -> **Create** -> **Pages** -> **Connect to Git**
   -> choose `MintMachineHQ/pSEOare`, production branch `gh-pages`.
   Or skip Git integration and deploy from CI instead (steps 3-5).
2. Create the API token: **My Profile -> API Tokens -> Create Token** ->
   *Cloudflare Pages: Edit*. Copy it once.
3. Add repository secrets: `CF_API_TOKEN` and `CF_ACCOUNT_ID`
   (Cloudflare dashboard -> Workers & Pages -> your account -> the account id).
4. Run the **Deploy to Cloudflare Pages** workflow with the project name.
5. Set `config.json -> domain` to the resulting `https://<project>.pages.dev`, or attach a
   custom domain in the Pages dashboard and use that instead.

## Ad network sign-up

Two networks are worth trying; run both, they fill each other's blank ad slots.

| Network | Sign up | Payout | Notes |
| --- | --- | --- | --- |
| Adsterra | adsterra.com -> Publisher -> Self-Serve | USDT TRC20, or Binance Pay | no minimum, approval can be strict on new domains |
| Monetag | monetag.com -> Publisher | USDT TRC20 | asks for traffic proof on some accounts |

Order of operations, because approval depends on it:

1. Get the domain live (GitHub Pages is fine to start).
2. Wait until Search Console shows impressions. Networks that check traffic will reject a
   zero-impression domain, and a rejection can cost a review cycle.
3. Sign up, add the domain exactly as published, and copy the `<script>` tag.
4. Paste it into `config.json -> monetization`:

```json
"adsterra_script": "<script async src=\"//...\" data-slot=\"...\"></script>"
```

The tag is injected into all three slots (top, mid, foot) and the waterfall swaps in the
CPA creative when a slot renders blank. Set the payout method to **USDT on TRC20** and paste
the Tron address from `config.json -> payouts.wallets.tron_trc20.address`.

## Payouts

All ad-network payouts go to one receive address, recorded in `config.json` so it is never
typed by hand:

| Field | Value |
| --- | --- |
| Address | `F9diFVYZh7SsC2KLGDH354gyyj2fCaLkTWCyRPHYxRwA` |
| Network | Solana |
| Asset | USDT |
| TRC20 fallback | `TPMycsdifyCGHyNRC8BeC12bS7ChA64BRN` (matches the Adsterra/Monetag payout rail) |

Both are receive addresses, so they are public information and safe to commit. No private key
lives in this repository. The TRON key is stored outside it, in
`~/.secrets/tron-wallet.json` with mode `600`, and `.gitignore` blocks any `*wallet*.json`.

**Network mismatch is the real risk here.** Adsterra and Monetag pay crypto mainly over
**TRC20 (Tron)**, and several payout options are Binance Pay or a bank transfer. A Solana
address cannot receive a TRC20 transfer, so before approving any payout, confirm the network
selector in the network's dashboard matches `Solana`. If they only offer TRC20, either keep a
second TRC20 address in this file or convert on withdrawal.

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
4. **IndexNow needs a root domain, and now has one.** The protocol validates its key at the
   domain root, so the earlier `github.io/repo/` subpath returned `403
   UserForbiddedToAccessSite`. On `pseoare.pages.dev` pings return `200/202`.
5. **Zero-competition does not mean traffic.** Ranking still needs authority, indexing and
   a few hundred indexed pages. The engine produces supply cheaply; demand is your problem.
6. **Adsterra/Monetag approval** requires real traffic on an established domain. Expect
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