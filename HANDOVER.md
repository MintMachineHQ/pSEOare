# pSEOare — System Handover

A programmatic-SEO revenue engine. One Python program scrapes public data APIs,
generates long-tail reference pages, publishes them to Cloudflare Pages, notifies
search engines, and monetizes the result with Adsterra ads.

This document explains what exists, how it fits together, and how to change it safely.

- **Live site:** <https://pseoare.pages.dev>
- **Repo:** <https://github.com/MintMachineHQ/pSEOare>
- **Total code:** ~4,100 lines across 21 tracked files

---

## 1. What the system does, end to end

```
 public data APIs          daily cron / manual dispatch
 (NASA, World Bank,        04:17 UTC
  nager.at, CoinGecko)
         │
         ▼
 ┌───────────────────┐
 │ 1. harvest        │  engine/sources/*.py — one module per dataset,
 │                   │  each writes a JSON snapshot to cache/
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐
 │ 2. build pages    │  engine/render.py — per-page HTML with
 │                   │  randomized DOM classes, JSON-LD, facts,
 │                   │  table, FAQ, related links, ad slots
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐
 │ 3. enrich prose   │  engine/enrich.py — Groq first, then Gemini,
 │                   │  then deterministic fallback. Never blocks a build.
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐
 │ 4. hub + sitemap  │  engine/hubs.py — paginated hub pages,
 │                   │  robots.txt, sitemap.xml, 404.html
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐   ┌──────────────────────────┐
 │ 5. notify         │──▶│ IndexNow (Bing/Yandex)   │
 │    engine/indexing│   │ Google Indexing API      │
 └─────────┬─────────┘   └──────────────────────────┘
           ▼
 ┌───────────────────┐
 │ 6. write + prune  │  engine/writer.py — manifest-based, so
 │                   │  each run only rewrites changed pages
 └─────────┬─────────┘
           ▼
   gh-pages branch  ──(workflow_run)──▶  Cloudflare Pages  ──▶  live site
```

Steps 1–6 run inside `.github/workflows/deploy.yml` on `ubuntu-latest`, capped at
25 minutes. The publish step pushes the `output/` directory to the `gh-pages` branch.
That push fires `deploy-cloudflare.yml`, which mirrors the same tree into Cloudflare
Pages. The mirror is what makes the site live; `gh-pages` is only a build artifact.

---

## 2. Repository map

| File | Lines | Responsibility |
| --- | --- | --- |
| `generator.py` | 155 | Orchestrator. Wires harvest → render → enrich → hub → notify → write. |
| `engine/config.py` | 109 | Loads and validates `config.json`, resolves secrets from the environment. |
| `engine/models.py` | 91 | `Page`, `Link`, `slugify`, content hashing. |
| `engine/http.py` | 128 | Shared `aiohttp` client: retries, timeouts, JSON cache read/write. |
| `engine/ratelimit.py` | 76 | Per-provider minimum interval and concurrency limiting. |
| `engine/enrich.py` | 480 | Prose/FAQ generation. Provider chain, model discovery, fallback copy. |
| `engine/render.py` | 523 | HTML for data pages, hub pages, JSON-LD, ads, waterfall JS, 404. |
| `engine/hubs.py` | 240 | Hub pagination, `sitemap.xml`, `robots.txt`, `404.html`. |
| `engine/writer.py` | 104 | Output directory, manifest, orphan pruning. |
| `engine/indexing.py` | 174 | IndexNow batches and the opt-in Google Indexing API. |
| `engine/sources/climate.py` | 148 | NASA POWER climatology, monthly temperature and rainfall. |
| `engine/sources/countries.py` | 276 | World Bank indicators, REST Countries shapes. |
| `engine/sources/crypto.py` | 147 | CoinGecko 365-day market history. |
| `engine/sources/holidays.py` | 164 | `date.nager.at`, holiday dates per country per year. |
| `tests/test_engine.py` | 675 | 51 tests covering rendering, ads, hubs, writer, sources. |
| `tools/adsterra_stats.py` | 163 | Read-only Adsterra revenue client. |
| `tools/generate_bitcoin_wallet.py` | 315 | BIP-84 wallet generator. Writes keys outside the repo. |

---

## 3. The four datasets

| Dataset | Source | Produces |
| --- | --- | --- |
| Holidays | `date.nager.at` | One page per country × holiday × year. 2 years back, 1 forward. |
| Countries | `api.worldbank.org` + REST Countries | One page per country: population, GDP, coordinates, currency. |
| Climate | NASA POWER climatology | One page per city: 12 monthly mean temps, rainfall, record highs/lows. 130 cities. |
| Crypto | CoinGecko `market_chart` | One page per coin: 365-day range, current price, drawdown. 8 coins. |

Every source caches its last successful response under `cache/`. If an upstream API
fails or blocks the runner, the cached snapshot is used and the page is marked
`cached snapshot` in the UI. A build never produces a blank page because of an outage.

**Dead ends, do not retry these:** `zippopotam.us` and `restcountries` v2 are retired,
Binance klines return 451, and CoinGecko caps key-free history at 365 days.

---

## 4. How a page is built

`engine/render.py` emits a complete static document. Deliberate choices:

- **Randomized DOM.** Class names and structural nesting vary per page
  (`seo.randomize_dom`). Thin programmatic pages that are byte-identical read as
  doorway pages; varying the markup is the cheapest defence.
- **One `<h1>`, self-referencing canonical, meta description capped at 158 chars.**
- **JSON-LD** with `Dataset`, `BreadcrumbList` and `FAQPage` graphs.
- **17–18 internal links per page** to sibling pages in the same dataset plus the hub.
- **Footer states the build timestamp**, which changes daily and gives crawlers a
  reason to re-fetch.

### Ads, and the rules that keep the account healthy

| Slot | Position | What loads |
| --- | --- | --- |
| `<head>` | once per page, before `</head>` | Adsterra popunder loader |
| `top` | after the facts block | CPA waterfall fallback |
| `mid` | after the sections | CPA waterfall fallback |
| `foot` | before the footer | Adsterra native banner, in a sized container |

Four rules are enforced by code and covered by tests:

1. **Page-wide loaders appear exactly once.** The popunder installs a document-level
   listener. Repeating it in every slot fires the popunder several times per visit and
   gets the account flagged. `head_ads()` is the only place it is emitted.
2. **The native banner appears in exactly one slot.** Its snippet ships a hardcoded
   container id (`container-<hash>`). Emitting it in top, mid and foot would duplicate
   that id and split impressions. The slot is chosen by
   `monetization.native_banner_slot`, default `foot`.
3. **The banner container is given a height** — 90px desktop, 250px under 820px. The
   vendor snippet is a bare `<div>`; at zero height it renders no creative and earns
   nothing.
4. **Both units have kill switches.** `monetization.popunder_enabled` and
   `monetization.native_banner_enabled` accept `false` to withdraw a unit in one config
   edit without losing its snippet. Absent means on.

The popunder belongs in `<head>`, which is what Adsterra documents. An
`appendChild`-on-null error is logged by Adsterra's own loader in that position; it was
verified to be benign, because the loader continues and still fires its impression
pixel. Body placement was tested and is worse: it skips the impression pixel entirely.

---

## 5. The build is incremental

`engine/writer.py` keeps a manifest of every page ever published. Each run:

1. Renders only the pages produced in this run.
2. Compares each page's content hash, including its prose, against the manifest.
3. Rewrites a file only when the hash changed. This is why an AI-enriched page is not
   overwritten by a later fallback-copy run.
4. Deletes any `*.html` in the output directory that is neither in the manifest nor in
   the keep set.

The keep set is fixed and includes `404.html`, `index.html`, `sitemap.xml`,
`robots.txt`, `.nojekyll`, plus every `hub-*` and `all-datasets-*` family and any
`seo.verification_files` entry. **`404.html` must stay in that set** — if the orphan
sweep removes it, the host falls back to serving `index.html` for every unknown path.

---

## 6. Search engine notification

**IndexNow** is enabled and working. The key is written to the output root as
`<key>.txt` and every new or changed URL is posted in batches of 10,000. Verified live:
a test batch returned HTTP 200. The key file must be reachable at the site root, which
is why publishing on `pages.dev` rather than a project subpath matters.

**The Google Indexing API is deliberately disabled.** Google restricts it to
`JobPosting` and `BroadcastEvent` resources. Submitting ordinary content pages through
it risks a manual action against the domain. Leave it off.

`changefreq` and `priority` in the sitemap are written for the benefit of other
crawlers; Google ignores both.

---

## 7. Monetization state, and what is actually blocking revenue

Adsterra approved `pseoare.pages.dev` on 2026-10-02. Domain id `6092146`.

| Placement | Id | Status | Impressions | Revenue |
| --- | --- | --- | --- | --- |
| `Popunder_1` | 31517989 | Active | 0 | $0.00 |
| `NativeBanner_1` | 31517990 | Active | 4 | $0.00 |

**The blocker is traffic, not configuration.** The site is crawlable, the markup is
clean, IndexNow is accepted, and the popunder demonstrably registers a click-away
(Adsterra's tracking pixel returned 200). There are simply no visitors.

Realistic arithmetic: popunder CPMs on cold, unbranded traffic sit well under $1, so
**$40/day needs on the order of 50,000 pageviews a day.** The site is one day old with
238 URLs and no backlinks. No ad setting closes that gap.

### The soft-404 bug that was found and fixed

Every unknown path used to answer with HTTP 200 and the full homepage. That hands a
crawler an unbounded set of URLs carrying byte-identical content, which spends crawl
budget on URLs that do not exist and suppresses indexing of the real pages. The engine
now writes a `noindex, follow` 404 document and the host returns a genuine 404. Verified
after deploy: unknown paths return 404, real pages still return 200.

### The "Other" category and adult creatives

The site is categorised **Other**, an unfiltered bucket with no vertical rules, so the
network served a suggestive dating creative in the native banner. The `Adult ads`
toggle was already off — that toggle governs the adult vertical only, not dating. The
banner was withdrawn with the kill switch pending a category change, which is a
dashboard action. The Adsterra **publisher API is GET-only**, so no API token can change
the category, the toggle, or campaign filters from outside the dashboard.

### Payout wallets

Recorded in `config.json` under `payouts.wallets`. Private keys and mnemonics live in
`~/.secrets/`, mode 600, and are never in the repository.

| Asset | Address | Use when |
| --- | --- | --- |
| USDT / TRC20 | `TPMycs…BRN` | Adsterra and Monetag default to this rail. |
| BTC | `bc1q0c2…kljxx` | Only if a dashboard offers Bitcoin as its own option. |
| USDT / Solana | `F9diF…HxRwA` | Not reachable from a TRC20 payout. |

A BTC address cannot receive USDT, and a Solana address cannot receive a TRC20
transfer. Match asset **and** network, every time.

---

## 8. Configuration reference

`config.json` is the only file most changes need.

| Key | Meaning |
| --- | --- |
| `domain` | Published origin. IndexNow and the sitemap depend on this being the real root. |
| `limits.max_pages_per_run` | Ceiling on pages per build, 300. |
| `limits.http_concurrency` | Parallel upstream requests, 8. |
| `limits.max_gemini_calls_per_run` | Enrichment budget, 40. |
| `sources.*.enabled` | Per-dataset on/off. |
| `sources.climate.cities` | City count, 130. |
| `sources.holidays.years_back` / `years_forward` | Window around the current year. |
| `gemini.groq_key_env` | Groq is tried first; its quota is far larger than Gemini's. |
| `monetization.popunder_script` | Page-wide loader, emitted once before `</head>`. |
| `monetization.adsterra_script` | Native banner snippet; parsed and placed in one slot. |
| `monetization.native_banner_slot` | Which slot carries the banner. Default `foot`. |
| `monetization.native_banner_enabled` | `false` withdraws the banner. |
| `monetization.popunder_enabled` | `false` withdraws the popunder. |
| `monetization.cpa_fallback_*` | What fills an empty slot. Still a placeholder. |
| `seo.verification_files` | Root-served tokens that survive pruning. |
| `payouts.wallets` | Addresses only. No keys, ever. |

### Secrets, all GitHub Actions secrets

| Secret | Used for |
| --- | --- |
| `GROQ_API_KEY` | Primary prose generation. |
| `GEMINI_API_KEY`, `GEMINI_API_KEYS` | Secondary providers, rotated. |
| `INDEXNOW_KEY` | IndexNow verification key. |
| `CF_API_TOKEN` | Cloudflare Pages mirror, in the mirror workflow only. |
| `GOOGLE_INDEXING_CREDENTIALS` | Unused while the indexing API is off. |

The Adsterra API token is **not** a CI secret. It lives at
`~/.secrets/adsterra-token` and is used only by hand via `tools/adsterra_stats.py`.

---

## 9. Running it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export GROQ_API_KEY=...        # optional; without it, fallback copy is used
export INDEXNOW_KEY=...        # optional; without it, notification is skipped

python3 generator.py --limit 20
python3 -m pytest tests/ -q
```

Useful flags: `--limit N` caps pages, `--dry-run` skips search-engine pings,
`--no-gemini` skips AI enrichment, `--config PATH` points at an alternate config.
Output lands in `output/`, which is a copy target for `gh-pages` and gitignored.

```bash
python3 tools/adsterra_stats.py 7        # revenue, per placement
python3 tools/adsterra_stats.py 30 --json # machine-readable
```

---

## 10. Conventions worth preserving

- **Never put a key in the repository, in `config.json`, or in chat.** Addresses are
  fine. Secrets go to `~/.secrets/` with mode 600, or to GitHub Actions secrets.
- **Every behaviour change needs a test.** The suite is the only thing standing between
  a fix and a silent regression in an unattended daily build.
- **Delta hashing must include the prose.** Omitting the copy hash means an
  AI-enriched page gets overwritten by fallback copy on the next run.
- **Verify with a second implementation.** Two wallet bugs passed self-consistent
  asserts and were only caught by cross-checking against `embit` and the published
  BIP-39/BIP-84 vectors.
- **Check the live site, not just the tests.** Every monetization claim in this
  document was confirmed against a real page load and the Adsterra API.

---

## 11. What still needs a human

1. **Change the Adsterra site category** away from `Other`, in the dashboard. The
   publisher API cannot do it.
2. **Submit the sitemap in Bing Webmaster Tools.** IndexNow already works, but Bing
   Webmaster Tools is the fastest route to indexation.
3. **Use Search Console URL Inspection → Request Indexing** on the ten hub pages. It is
   manual, and it is the single highest-impact action available.
4. **Replace the CPA fallback** with a real offer. `cpa_fallback_url` is
   `https://example.com/offer`, so the top and mid slots earn nothing.
5. **Get traffic.** A custom domain and genuine inbound links are the only durable
   route to the 50,000 pageviews a day that $40/day implies.
