#!/usr/bin/env python3
"""Render docs/architecture.svg.

Hand-maintaining a few hundred coordinates in raw SVG is how diagrams rot, so the
layout lives here as data instead. Re-run after changing the pipeline and commit
the result:

    python3 tools/make_architecture.py && rsvg-convert -w 1800 docs/architecture.svg -o docs/architecture.png
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "architecture.svg"

W, H = 2200, 3160
INK = "#e2e8f0"
MUTED = "#94a3b8"
DIM = "#64748b"
LINE = "#1e293b"
EDGE = "#334155"

PALETTE = {
    "src": ("#1e3a5f", "#2c5282"),
    "eng": ("#14532d", "#1d7a45"),
    "copy": ("#3b2f63", "#553c9a"),
    "render": ("#0c4a6e", "#0e7490"),
    "write": ("#3f3f46", "#52525b"),
    "disc": ("#4c1d95", "#6d28d9"),
    "host": ("#7c2d12", "#c2410c"),
    "money": ("#713f12", "#b45309"),
    "guard": ("#7f1d1d", "#991b1b"),
    "ok": ("#134e4a", "#0f766e"),
}


def esc(text: str) -> str:
    return html.escape(text, quote=True)


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.parts: list[str] = []
        self.width = width
        self.height = height

    def raw(self, markup: str) -> None:
        self.parts.append(markup)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str = "none",
        stroke: str = EDGE,
        rx: float = 10,
        sw: float = 2,
        dash: str | None = None,
    ) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'
        )

    def grad_rect(self, x: float, y: float, w: float, h: float, key: str, rx: float = 12, sw: float = 2) -> None:
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="url(#{key})" stroke="{PALETTE[key][1]}" stroke-width="{sw}"/>'
        )

    def text(
        self,
        x: float,
        y: float,
        body: str,
        size: int = 15,
        fill: str = INK,
        weight: str = "normal",
        anchor: str = "start",
        opacity: float = 1.0,
    ) -> None:
        self.parts.append(
            f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" opacity="{opacity}">{esc(body)}</text>'
        )

    def wrap(self, x: float, y: float, body: str, size: int = 13, fill: str = MUTED, width: int = 62, lh: int = 17) -> float:
        """Greedy wrap. Returns the y after the last line."""
        for i, line in enumerate(wrap_lines(body, width)):
            self.text(x, y + i * lh, line, size, fill)
        return y + max(1, len(wrap_lines(body, width))) * lh

    def line(self, x1: float, y1: float, x2: float, y2: float, stroke: str = EDGE, sw: float = 2, dash: str | None = None) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"{d}/>'
        )

    def path(self, d: str, stroke: str = EDGE, sw: float = 2, fill: str = "none", dash: str | None = None, arrow: bool = True) -> None:
        da = f' stroke-dasharray="{dash}"' if dash else ""
        ma = ' marker-end="url(#ar)"' if arrow else ""
        self.parts.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{da}{ma}/>')

    def render(self) -> str:
        return "\n".join(self.parts)


def wrap_lines(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        if len(cur) + len(word) + 1 <= width or not cur:
            cur = f"{cur} {word}".strip()
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def stage(c: Canvas, num: int, title: str, sub: str, y: float) -> None:
    """Stage band header."""
    c.rect(60, y, W - 120, 46, fill="#0a1220", stroke=LINE, rx=8, sw=1)
    c.text(84, y + 30, f"STAGE {num}", 14, "#38bdf8", "bold")
    c.text(180, y + 30, title, 19, INK, "bold")
    c.text(180 + len(title) * 11 + 26, y + 29, sub, 13, DIM)


def box(
    c: Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    key: str,
    title: str,
    lines: list[str],
    badge: str | None = None,
    tsize: int = 16,
) -> None:
    c.grad_rect(x, y, w, h, key)
    c.text(x + 18, y + 30, title, tsize, INK, "bold")
    yy = y + 52
    for ln in lines:
        c.text(x + 18, yy, ln, 12, MUTED)
        yy += 16
    if badge:
        bw = 9 * len(badge) + 18
        c.rect(x + w - bw - 12, y + 12, bw, 22, fill="#0b1220", stroke=PALETTE[key][1], rx=11, sw=1)
        c.text(x + w - bw / 2 - 12, y + 27, badge, 12, "#7dd3fc", "bold", "middle")


def build() -> str:
    c = Canvas(W, H)

    # ---------------------------------------------------------------- defs
    defs = ['<defs>']
    defs.append(
        '<linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">'
        '<stop offset="0%" stop-color="#0b1220"/><stop offset="55%" stop-color="#0d1526"/>'
        '<stop offset="100%" stop-color="#0a101d"/></linearGradient>'
    )
    for key, (a, b) in PALETTE.items():
        defs.append(
            f'<linearGradient id="{key}" x1="0" y1="0" x2="0.9" y2="1">'
            f'<stop offset="0%" stop-color="{a}"/><stop offset="100%" stop-color="{b}"/></linearGradient>'
        )
    defs.append(
        '<marker id="ar" markerWidth="12" markerHeight="12" refX="10" refY="4.5" orient="auto">'
        '<path d="M0,0 L10,4.5 L0,9 z" fill="#94a3b8"/></marker>'
    )
    defs.append(
        '<marker id="arg" markerWidth="12" markerHeight="12" refX="10" refY="4.5" orient="auto">'
        '<path d="M0,0 L10,4.5 L0,9 z" fill="#0f766e"/></marker>'
    )
    defs.append(
        '<marker id="arm" markerWidth="12" markerHeight="12" refX="10" refY="4.5" orient="auto">'
        '<path d="M0,0 L10,4.5 L0,9 z" fill="#b45309"/></marker>'
    )
    defs.append('</defs>')
    c.raw("\n".join(defs))
    c.raw(f'<rect width="{W}" height="{H}" fill="url(#bg)"/>')

    # ---------------------------------------------------------------- title
    c.text(60, 74, "pSEOare", 44, INK, "bold")
    c.text(292, 74, "LongTail Data Atlas", 26, "#38bdf8", "bold")
    c.text(60, 106, "Programmatic SEO revenue engine · full pipeline, every part and how they connect", 17, MUTED)
    c.text(W - 60, 74, "github.com/MintMachineHQ/pSEOare", 14, DIM, anchor="end")
    c.text(W - 60, 98, "live: pseoare.pages.dev", 14, "#34d399", anchor="end")

    # live stat strip
    stats = [
        ("1,907", "published pages"),
        ("5", "data sources"),
        ("1,351", "cities"),
        ("24", "coins"),
        ("04:17 UTC", "daily cron"),
        ("300", "pages / run cap"),
        ("$0.0010", "revenue so far"),
        ("83", "tests green"),
    ]
    sx, sw_, sy = 60, 255, 126
    for i, (big, small) in enumerate(stats):
        x = sx + (i % 4) * sw_
        y = sy + (i // 4) * 62
        c.rect(x, y, sw_ - 14, 52, fill="#0b1220", stroke=LINE, rx=8, sw=1)
        c.text(x + 14, y + 25, big, 21, "#7dd3fc", "bold")
        c.text(x + 14, y + 43, small, 12, DIM)
    c.line(60, 258, W - 60, 258, LINE, 2)

    # ------------------------------------------------------- stage 0 schedule
    stage(c, 0, "SCHEDULE", "what starts everything", 286)
    box(c, 60, 348, 500, 116, "src", "GitHub Actions · deploy.yml", [
        "cron 17 4 * * *  (off the top of the hour on purpose)",
        "plus workflow_dispatch for manual runs",
        "rolls cache/ in, builds, publishes gh-pages",
    ], badge="DAILY")
    box(c, 596, 348, 500, 116, "src", "GitHub Actions · deploy-cloudflare.yml", [
        "fires on workflow_run, after the build finishes",
        "mirrors the gh-pages artifact to Cloudflare Pages",
        "verifies pseoare.pages.dev answers 200",
    ], badge="MIRROR")
    box(c, 1132, 348, 1008, 116, "src", "Cloudflare Pages project", [
        "production branch: gh-pages   ·   CF_API_TOKEN held as a GitHub secret only",
        "serves clean URLs, so config seo.url_style = \"clean\" and no .html in the live path",
        "chosen because it is free at this scale and needs no billing card",
    ], badge="HOSTING")

    # ------------------------------------------------------- stage 1 sources
    stage(c, 1, "DATA SOURCES", "four upstream APIs and one derived file, all free, all keyed by nothing", 500)
    src_y, src_h = 562, 250
    srcs = [
        ("src", "NASA POWER", [
            "power.larc.nasa.gov",
            "/temporal/climatology/point",
            "",
            "monthly T2M / rainfall / wind /",
            "solar radiation, per lat+lon",
            "",
            "1,351 cities, population ≥ 250k",
            "paced and budgeted before fetch",
            "30 max per country",
        ], "75 / run"),
        ("src", "Nager.Date", [
            "date.nager.at/api/v3",
            "/PublicHolidays/{cc}/{year}",
            "",
            "no API key, no registration",
            "",
            "10 years back + 1 forward",
            "country x year matrix",
        ], "75 / run"),
        ("src", "World Bank", [
            "api.worldbank.org/v2",
            "/country/all/indicator/",
            "",
            "population, area, GDP,",
            "GDP per capita",
            "",
            "profile + population + rank",
        ], "as budget allows"),
        ("src", "CoinGecko", [
            "api.coingecko.com/api/v3",
            "/coins/{id}/market_chart",
            "",
            "365-day monthly ranges",
            "",
            "24 coins, capped 8 req/min",
            "public key-free tier",
        ], "16 / run"),
        ("src", "GeoNames + curated", [
            "download.geonames.org",
            "cities15000.zip + countryInfo",
            "",
            "offline file, no API calls",
            "",
            "351 cities added, 144 countries,",
            "latitude spread checked",
        ], "one-off"),
    ]
    bw = (W - 120 - 4 * 18) / 5
    for i, (key, name, lines, badge) in enumerate(srcs):
        box(c, 60 + i * (bw + 18), src_y, bw, src_h, key, name, lines, badge=badge, tsize=17)

    # budget note under sources
    c.rect(60, 830, W - 120, 62, fill="#101c14", stroke="#1d7a45", rx=10, sw=1, dash="6 4")
    c.text(84, 856, "CALL BUDGET", 14, "#4ade80", "bold")
    c.text(224, 856, "max_pages_per_run 300 · http_concurrency 8 · timeout 30s · 3 retries · every source spends from one shared budget,", 13, MUTED)
    c.text(84, 878, "unpublished pages are served first, so each run grows the corpus instead of refreshing pages that are already perfect. Known pages get the leftovers, which is how upstream changes still get picked up.", 13, MUTED)

    # arrows into stage 2
    for i in range(5):
        x = 60 + i * (bw + 18) + bw / 2
        c.path(f"M{x} {src_y + src_h} L{x} 940", EDGE, 2)

    # ------------------------------------------------------- stage 2 engine
    stage(c, 2, "THE ENGINE", "generator.py orchestrates; each module owns one job", 922)
    e_y, e_h = 984, 216
    eng = [
        ("eng", "Collect + waterfall", [
            "sources/*.py each expose collect()",
            "a failing source logs and returns []",
            "one bad API can never stop a run",
            "delta cache: only new or changed",
            "rows become pages",
        ]),
        ("eng", "Reserve + cache", [
            "HTTP layer caches every payload",
            "cache/climate_*.json, last_scrape,",
            "entity_index.json",
            "if upstream dies, the cached value",
            "still renders a full page",
        ]),
        ("copy", "Enrichment chain", [
            "1. cached copy from a past run",
            "2. Groq  (quota permitting)",
            "3. Gemini keys, rotated",
            "4. deterministic fallback copy",
            "40 calls/run, 7.5s spacing",
        ]),
        ("render", "Render", [
            "engine/render.py + hubs.py",
            "JSON-LD Dataset schema",
            "DOM/CSS variation per page",
            "hub pagination at 250 links",
            "canonical + breadcrumbs + H1",
        ]),
    ]
    ew = (W - 120 - 3 * 18) / 4
    for i, (key, name, lines) in enumerate(eng):
        box(c, 60 + i * (ew + 18), e_y, ew, e_h, key, name, lines)
        if i < 3:
            x1 = 60 + i * (ew + 18) + ew
            c.path(f"M{x1} {e_y + e_h / 2} L{x1 + 18} {e_y + e_h / 2}", EDGE, 2)

    # fallback callout
    c.rect(60, 1218, W - 120, 74, fill="#14102a", stroke="#553c9a", rx=10, sw=1)
    c.text(84, 1244, "THE COPY WATERFALL", 14, "#c4b5fd", "bold")
    c.wrap(84, 1266, "Every page must have prose, so the chain degrades instead of failing. Groq and Gemini are daily-capped and go over most days, but the last rung generates the paragraph from that page's own numbers - actual temperature spread, wet and dry months, annual rainfall - with a stable hash so wording varies per slug and never repeats. The corpus is therefore never thin, even with every AI key exhausted, which is what actually happened on 2026-10-02.", 12, MUTED, width=150)

    for i in range(4):
        x = 60 + i * (ew + 18) + ew / 2
        c.path(f"M{x} {1218 + 74} L{x} 1340", EDGE, 2)

    # ------------------------------------------------------- stage 3 write
    stage(c, 3, "WRITE + MANIFEST", "what makes the build incremental", 1322)
    w_y, w_h = 1384, 190
    wboxes = [
        ("write", "writer.py", [
            "content_hash per page decides",
            "whether to rewrite at all",
            "",
            "hash includes the data AND",
            "seo.prose_version, so bumping it",
            "refreshes the whole corpus once",
        ]),
        ("write", "manifest.json", [
            "every published page, not just",
            "this run's slice",
            "",
            "anything corpus-wide (sitemap,",
            "budget, orphans) must read this,",
            "never the current page list",
        ]),
        ("write", "Orphan sweep", [
            "keeps index / 404 / sitemap /",
            "feed / robots / .nojekyll",
            "",
            "anything not in the manifest and",
            "not on that list is deleted, so",
            "renames never leave dead pages",
        ]),
    ]
    ww = (W - 120 - 2 * 18) / 3
    for i, (key, name, lines) in enumerate(wboxes):
        box(c, 60 + i * (ww + 18), w_y, ww, w_h, key, name, lines)
        if i < 2:
            x1 = 60 + i * (ww + 18) + ww
            c.path(f"M{x1} {w_y + w_h / 2} L{x1 + 18} {w_y + w_h / 2}", EDGE, 2)

    # feedback loop arrow, right to left
    c.path(
        "M60 1510 C 40 1510 40 1500 40 1470 L40 1290 C40 1250 60 1250 100 1250 L100 1218",
        "arg", 2, dash="7 5",
    )
    c.raw(
        '<g transform="translate(38,1400) rotate(-90)"><text x="0" y="0" fill="#0f766e" '
        'font-size="12" text-anchor="middle">manifest decides what the next run rebuilds</text></g>'
    )

    # ------------------------------------------------------- stage 4 discovery
    stage(c, 4, "DISCOVERY", "how a page gets found", 1614)
    d_y, d_h = 1676, 172
    disc = [
        ("disc", "sitemap.xml", ["built from the manifest", "every published URL, all 1,907", "points at the live host, not the artifact"]),
        ("disc", "IndexNow", ["150 URLs per run", "current run deferred to next run, so", "Bing is never told before publish", "quota-free, replaces manual submission"]),
        ("disc", "feed.xml", ["RSS 2.0, newest 200 pages", "the path aggregators actually", "subscribe to", "listed in robots.txt"]),
        ("disc", "robots.txt", ["allow all", "declares sitemap.xml", "names feed.xml", "genuine 404 carries noindex"]),
    ]
    for i, (key, name, lines) in enumerate(disc):
        box(c, 60 + i * (dw := ((W - 120 - 3 * 18) / 4)) + 0, d_y, dw, d_h, key, name, lines)
        c.path(f"M{60 + i * (dw + 18) + dw / 2} {d_y + d_h} L{60 + i * (dw + 18) + dw / 2} 1890", EDGE, 2)

    # ------------------------------------------------------- stage 5 publish
    stage(c, 5, "PUBLISH", "gh-pages is the artifact, Cloudflare is the front door", 1872)
    box(c, 60, 1934, 700, 130, "host", "gh-pages branch", [
        "committed by the build, every page",
        "a plain static file, no server",
        "",
        "never advertised: clean URLs would not resolve there",
    ], badge="ARTIFACT")
    box(c, 796, 1934, 700, 130, "host", "Cloudflare Pages", [
        "reads gh-pages, serves at the edge",
        "free tier, no card, global CDN",
        "",
        "custom domain planned once the site earns enough to justify it",
    ], badge="LIVE")
    box(c, 1532, 1934, 608, 130, "host", "Health check", [
        "deploy-cloudflare.yml verifies",
        "HTTP 200 after every mirror",
        "",
        "40/40 random pages returned 200 on the last audit",
    ], badge="VERIFIED")
    c.path("M760 1999 L796 1999", EDGE, 2)
    c.path("M1496 1999 L1532 1999", EDGE, 2)

    # ------------------------------------------------------- stage 6 the site
    stage(c, 6, "THE LIVE SITE", "1,907 pages, how a visitor moves through them", 2104)
    s_y, s_h = 2166, 214
    sites = [
        ("ok", "Entry points", [
            "index.html lists every dataset",
            "hub-climate, hub-holidays,",
            "hub-countries, hub-crypto",
            "paginated at 250 links each",
        ]),
        ("ok", "Content pages", [
            "city climate, country data,",
            "holiday calendars, crypto ranges",
            "",
            "each with JSON-LD, canonical,",
            "table data and real prose",
        ]),
        ("ok", "Cross-dataset linking", [
            "a climate page links to that",
            "country's holidays and data",
            "via cache/entity_index.json",
            "raises pageviews per session,",
            "which is what an impression needs",
        ]),
        ("ok", "Genuine 404", [
            "unknown path returns 404, not the",
            "homepage with a 200",
            "",
            "a soft 404 here would have",
            "handed crawlers infinite duplicates",
        ]),
    ]
    sw2 = (W - 120 - 3 * 18) / 4
    for i, (key, name, lines) in enumerate(sites):
        box(c, 60 + i * (sw2 + 18), s_y, sw2, s_h, key, name, lines)

    # ------------------------------------------------------- stage 7 money
    stage(c, 7, "MONEY", "the only reason any of this exists", 2420)
    m_y, m_h = 2482, 168
    box(c, 60, m_y, 520, m_h, "money", "Visitor arrives", [
        "from search, after Bing or Google",
        "indexes the sitemap and the feed",
        "",
        "this is the step that does not",
        "happen on its own: it needs time,",
        "and backlinks the engine cannot make",
    ])
    box(c, 616, m_y, 520, m_h, "money", "Adsterra popunder", [
        "one loader before </head>",
        "abscloud.org, domain 6092146,",
        "placement 31517989",
        "",
        "fires the impression pixel; a",
        "click-away posts to araplhn.org",
    ])
    box(c, 1172, m_y, 480, m_h, "money", "House ads", [
        "any slot no network fills gets a",
        "first-party link to the index",
        "",
        "native banner is withdrawn: the",
        "\"Other\" category served suggestive",
        "creatives",
    ])
    box(c, 1688, m_y, 452, m_h, "money", "Payout", [
        "TRC20 USDT on Tron is the default",
        "",
        "watch the network: a Solana address",
        "cannot receive a Tron payout",
        "",
        "earned so far: $0.0010",
    ])
    for x in (580, 1136, 1652):
        c.path(f"M{x} {m_y + m_h / 2} L{x + 36} {m_y + m_h / 2}", EDGE, 2)

    # ------------------------------------------------------- guardrails
    stage(c, 8, "GUARDRAILS", "what stops this from becoming a spam or a soft-404 site", 2692)
    g_y, g_h = 2754, 146
    guards = [
        ("guard", "Real 404, noindex", ["unknown path returns 404", "with noindex, follow"]),
        ("guard", "Ad kill switch", ["monetization.native_banner_", "enabled = false"]),
        ("guard", "House ad target check", ["must point at a path that", "really exists"]),
        ("guard", "Quota-aware", ["daily-cap 429 is not", "retried, it is bypassed", "for the rest of the day"]),
        ("guard", "Empty responses", ["a 200 with an empty body is", "success, not a parse failure", "(this is what broke IndexNow)"]),
        ("guard", "Secrets", ["API keys live in ~/.secrets", "and GitHub secrets only,", "never in the repo or chat"]),
    ]
    gw = (W - 120 - 5 * 16) / 6
    for i, (key, name, lines) in enumerate(guards):
        box(c, 60 + i * (gw + 16), g_y, gw, g_h, key, name, lines, tsize=14)

    # ------------------------------------------------------- blockers
    c.rect(60, 2936, W - 120, 128, fill="#1a0f14", stroke="#7f1d1d", rx=12, sw=2)
    c.text(84, 2964, "WHAT IS ACTUALLY BLOCKING REVENUE  (the code is not the problem)", 15, "#fca5a5", "bold")
    items = [
        ("Indexing", "Bing shows zero results for the domain and Search Console reported zero indexed pages. Content is ready; discovery is waiting."),
        ("Volume", "$40/day needs tens of thousands of daily pageviews at cold-site CPMs. The engine can serve traffic, it cannot manufacture it."),
        ("Backlinks", "Cannot be automated. A custom domain and genuine outreach are the honest ceiling, and both are on the list above."),
    ]
    for i, (label, body) in enumerate(items):
        y = 2990 + i * 26
        c.text(84, y, label, 13, "#fca5a5", "bold")
        c.text(180, y, body, 12.5, MUTED)

    c.text(60, 3100, "pSEOare · engine/config.json is the single source of truth for every number on this diagram", 12, "#475569")
    c.text(W - 60, 3100, "regenerate: python3 tools/make_architecture.py", 12, "#475569", anchor="end")

    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="DejaVu Sans, Helvetica, Arial, sans-serif">\n{c.render()}\n</svg>\n'


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), "utf-8")
    print(f"wrote {OUT}")