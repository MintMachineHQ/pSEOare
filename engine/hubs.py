"""Internal linking, hub pages, pagination and sitemap generation."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from typing import Any

from .config import Config
from .models import Link, Page
from .render import _esc, render_hub

log = logging.getLogger(__name__)

SECTIONS: dict[str, dict[str, str]] = {
    "holidays_year": {"slug": "hub-holidays", "h1": "Public holiday calendars by country and year"},
    "holiday_single": {"slug": "hub-single-holidays", "h1": "Individual public holidays by date"},
    "climate_city": {"slug": "hub-climate", "h1": "Monthly climate averages by city"},
    "country_profile": {"slug": "hub-countries", "h1": "Country facts, population and currency"},
    "country_population": {"slug": "hub-population", "h1": "Population by country"},
    "country_rank": {"slug": "hub-countries", "h1": "Country facts, population and currency"},
    "crypto_12m": {"slug": "hub-crypto", "h1": "Crypto price by month"},
}
RELATED_PER_PAGE = 12
# Links per hub page. Kept well under the 50k-link-per-page search limit while
# staying small enough to paginate once the corpus passes a few thousand pages.
LINKS_PER_PAGE = 250


def _hub_filename(slug: str, index: int) -> str:
    return f"{slug}.html" if index == 1 else f"{slug}-{index}.html"


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# Cap on cross-dataset links. Same-kind siblings already fill the common case, and these
# are worth adding mainly for the pages that would otherwise be a dead end, such as a
# climate page for a city with no holiday coverage.
CROSS_ENTITY_LINKS = 3
# Cap per country, so one heavily covered country cannot crowd out the rest.
ENTITY_INDEX_PER_ENTITY = 8


def load_entity_index(cache_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Published pages carry a country, but a single run only rebuilds one slice of the
    corpus, so a run of climate pages cannot see the holiday pages built weeks ago. This
    map, accumulated across runs, is what lets cross-dataset links point at them.
    """
    path = Path(cache_dir) / "entity_index.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_entity_index(cache_dir: Path, pages: list[Page]) -> None:
    index = load_entity_index(cache_dir)
    for page in pages:
        entity = str(page.data.get("entity") or "").strip()
        if not entity:
            continue
        bucket = index.setdefault(entity, [])
        if not any(entry["path"] == page.path for entry in bucket):
            bucket.append({"path": page.path, "kind": page.kind, "h1": page.h1})
    # Bound it: one bucket per country is small, but guard against unbounded growth anyway.
    for entity, bucket in list(index.items()):
        if len(bucket) > ENTITY_INDEX_PER_ENTITY:
            index[entity] = sorted(bucket, key=lambda e: e["path"])[:ENTITY_INDEX_PER_ENTITY]
    path = Path(cache_dir) / "entity_index.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, sort_keys=True), "utf-8")
    except OSError as exc:  # a missing index only costs cross links, never the build
        log.warning("could not write entity index: %s", exc)


def assign_related(
    pages: list[Page], cfg: Config, index: dict[str, list[dict[str, str]]] | None = None
) -> None:
    """Link every page to its section hub, rotating siblings, and pages about its country.

    Sibling links alone keep visitors inside one dataset, so a Shanghai climate page never
    leads anywhere near the Shanghai holiday calendar. Both are answering questions about
    the same place, so linking across datasets is the cheapest way to raise pageviews per
    session, which is what the popunder impression depends on.
    """
    by_kind: dict[str, list[Page]] = defaultdict(list)
    by_entity: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_kind[page.kind].append(page)
        entity = str(page.data.get("entity") or "").strip()
        if entity:
            by_entity[entity].append(
                {"path": page.path, "kind": page.kind, "h1": page.h1}
            )
    # Pages published by earlier runs, so a climate-only run can still reach holiday and
    # country pages. Anything stale is harmless: the link check below would just miss.
    for entity, entries in (index or {}).items():
        known = {entry["path"] for entry in by_entity.get(entity, [])}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("path") not in known:
                by_entity[entity].append(entry)

    for page in pages:
        section = SECTIONS.get(page.kind)
        links: list[Link] = []
        if section:
            links.append(Link(f"All: {section['h1']}", cfg.url_for(f"{section['slug']}.html")))
        entity = str(page.data.get("entity") or "").strip()
        cross = [
            entry
            for entry in by_entity.get(entity, [])
            if entry["path"] != page.path and entry["kind"] != page.kind
        ]
        for entry in cross[:CROSS_ENTITY_LINKS]:
            links.append(Link(entry["h1"], cfg.url_for(entry["path"])))
        siblings = [p for p in by_kind[page.kind] if p.slug != page.slug]
        # Stable pseudo-rotation: same page keeps the same neighbours every build.
        offset = int(hashlib_offset(page.slug)) % max(1, len(siblings))
        rotated = siblings[offset:] + siblings[:offset]
        for sib in rotated[:RELATED_PER_PAGE]:
            links.append(Link(sib.h1, cfg.url_for(sib.path)))
        page.related = links


def hashlib_offset(text: str) -> int:
    """Process-independent hash: random offsets would reshuffle links every build."""
    total = 0
    for ch in text:
        total = (total * 131 + ord(ch)) & 0xFFFFFFFF
    return total


def hub_documents(
    pages: list[Page], cfg: Config, theme: dict, css: str, index_extra: str = ""
) -> list[tuple[str, str]]:
    """Return [(filename, html)] for the root index plus a paginated hub per section."""
    stamp = datetime.now(timezone.utc).isoformat()
    docs: list[tuple[str, str]] = []
    by_kind: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_kind[page.kind].append(page)

    everything = sorted(pages, key=lambda p: (p.kind, p.h1))
    index_pages = _chunks(
        [Link(p.h1, cfg.url_for(p.path)) for p in everything], LINKS_PER_PAGE
    )
    docs.append(
        (
            "index.html",
            render_hub(
                title=f"{cfg.site_name} - free public data reference",
                h1=f"{cfg.site_name}: {len(pages)} automatically updated data pages",
                intro=(
                    f"This site publishes {len(pages)} data pages built from public APIs on a "
                    "daily schedule. Every value is sourced, dated and linked to neighbouring "
                    "datasets so you can compare years, cities or countries without digging "
                    "through spreadsheets."
                ),
                links=index_pages[0],
                cfg=cfg,
                theme=theme,
                css=css,
                stamp=stamp,
                canonical="index.html",
                section_title="All datasets",
                extra_body=index_extra,
            ),
        )
    )
    for offset, links in enumerate(index_pages[1:], start=2):
        docs.append(
            (
                f"all-datasets-{offset}.html",
                render_hub(
                    title=f"All datasets page {offset} | {cfg.site_name}",
                    h1="All datasets",
                    intro=f"Page {offset} of the complete dataset index.",
                    links=links,
                    cfg=cfg,
                    theme=theme,
                    css=css,
                    stamp=stamp,
                    canonical=f"all-datasets-{offset}.html",
                    section_title="All datasets",
                ),
            )
        )

    # Country profile and population share a hub slug, so group by slug not by kind.
    grouped: dict[str, list[Page]] = defaultdict(list)
    for kind, meta in SECTIONS.items():
        grouped[meta["slug"]].extend(by_kind.get(kind, []))

    for slug, items in grouped.items():
        if not items:
            continue
        items = sorted(items, key=lambda p: p.h1)
        meta = next(m for m in SECTIONS.values() if m["slug"] == slug)
        chunks = _chunks([Link(p.h1, cfg.url_for(p.path)) for p in items], LINKS_PER_PAGE)
        for index, links in enumerate(chunks, start=1):
            docs.append(
                (
                    _hub_filename(slug, index),
                    render_hub(
                        title=f"{meta['h1']} | {cfg.site_name}",
                        h1=meta["h1"] if index == 1 else f"{meta['h1']} (page {index})",
                        intro=(
                            f"{len(items)} pages in this collection. Each page carries its own "
                            "summary, key figures and FAQ, and links to the closest matching "
                            "datasets."
                        ),
                        links=links,
                        cfg=cfg,
                        theme=theme,
                        css=css,
                        stamp=stamp,
                        canonical=_hub_filename(slug, index),
                        section_title=meta["h1"],
                        page_index=index,
                        total_pages=len(chunks),
                    ),
                )
            )
    return docs


def hub_filenames(pages: list[Page]) -> list[str]:
    """Filenames of every generated hub/index page, for sitemap and cleanup."""
    by_kind: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_kind[page.kind].append(page)
    names = ["index.html"]
    grouped: dict[str, list[Page]] = defaultdict(list)
    for kind, meta in SECTIONS.items():
        grouped[meta["slug"]].extend(by_kind.get(kind, []))
    for slug, items in grouped.items():
        if not items:
            continue
        for index in range(1, len(_chunks(items, LINKS_PER_PAGE)) + 1):
            names.append(_hub_filename(slug, index))
    if len(pages) > LINKS_PER_PAGE:
        for index in range(2, len(_chunks(pages, LINKS_PER_PAGE)) + 1):
            names.append(f"all-datasets-{index}.html")
    return names


def sitemap_xml(
    pages: list[Page],
    cfg: Config,
    stamp: str,
    published: dict[str, Any] | None = None,
    extra: list[str] | None = None,
    extra_dirs: set[str] | None = None,
) -> str:
    """Sitemap for the whole published corpus.

    ``pages`` is only the slice this run produced. The engine is incremental, so passing
    just that slice made the sitemap list a few hundred URLs out of several hundred
    published ones, and rotate its contents daily. Crawlers are then told about a
    fraction of the site, and any URL dropped from the file risks being dropped from the
    index. ``published`` is the manifest, keyed by path, and supplies lastmod dates for
    everything not rebuilt today.
    """
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    from xml.sax.saxutils import escape as xml_escape

    urls = [(name, "0.8", "daily", stamp) for name in hub_filenames(pages)]
    rebuilt = {page.path: page for page in pages}
    for path, entry in (published or {}).items():
        if path in rebuilt or not path.endswith(".html"):
            continue
        generated = (entry or {}).get("generated_at") or stamp
        urls.append((path, "0.7", "weekly", generated))
    urls += [(p.path, "0.7", "weekly", stamp) for p in pages]

    # Derived surfaces: rankings, comparisons, the Today page, the API docs, the widget
    # pages and the JSON endpoints. They are not Page objects, so they are passed in as
    # paths. Rankings and Today change whenever the underlying figures do, so they carry
    # a daily changefreq; the API files change with the build too. Widgets and API
    # documents are stable, so they are weekly.
    daily, weekly = [], []
    for path in extra or []:
        if path.startswith("top-") or path in ("today.html", "all-datasets-1.html"):
            daily.append(path)
        elif path.endswith(".json"):
            daily.append(path)
        else:
            weekly.append(path)
    urls += [(p, "0.9", "daily", stamp) for p in daily]
    urls += [(p, "0.6", "weekly", stamp) for p in weekly]
    # Directories such as api/ appear in the sitemap through their files; listing the
    # directory itself would be a URL that serves a directory listing.
    urls = [u for u in urls if u[0] not in (extra_dirs or set())]

    seen: set[str] = set()
    entries = []
    for path, priority, freq, lastmod in urls:
        if path in seen:
            continue
        seen.add(path)
        entries.append(
            f"  <url><loc>{xml_escape(cfg.url_for(path))}</loc>"
            f"<lastmod>{lastmod[:10]}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )
    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{body}
</urlset>
"""


AI_CRAWLERS = (
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
    "anthropic-ai", "PerplexityBot", "Perplexity-User", "Google-Extended",
    "Applebot-Extended", "CCBot", "Bingbot", "DuckAssistBot",
)


def robots_txt(cfg: Config) -> str:
    """robots.txt, with the AI crawlers named explicitly.

    ``User-agent: *`` already allows them, so the explicit list is not needed to permit
    anything. It is here because these agents are the ones actually indexing new hosts
    quickly right now, and naming them makes the intent reviewable instead of implicit.
    Google-Extended is granted separately from Googlebot on purpose: it governs Gemini
    training and AI Overviews use, not classic search ranking.
    """
    lines = []
    for agent in AI_CRAWLERS:
        lines.append(f"User-agent: {agent}\nAllow: /\n")
    return (
        "".join(lines)
        + "User-agent: *\nAllow: /\n"
        f"Sitemap: {cfg.url_for('sitemap.xml')}\n"
        f"# RSS feed: {cfg.url_for('feed.xml')}\n"
        f"# Plain-text guide for AI agents: {cfg.url_for('llms.txt')}\n"
    )


def llms_txt(cfg: Config) -> str:
    """A plain-text map of the site for language-model crawlers.

    The llms.txt convention is a Markdown index of what a site publishes and where. It
    costs one file, needs no submission anywhere, and is the cheapest way to make a large
    generated corpus legible to an agent that would otherwise have to guess our structure.
    """
    lines = [
        f"# {cfg.site_name}",
        "",
        f"> {cfg.raw.get('site_description') or 'Reference pages rebuilt daily from public data APIs.'}",
        "",
        f"{cfg.domain}/",
        "",
        "Every page below is rebuilt daily from public data APIs and cites its source.",
        "",
        "## Dataset hubs",
        "",
    ]
    for slug, title in sorted({s["slug"]: s["h1"] for s in SECTIONS.values()}.items()):
        lines.append(f"- [{title}]({cfg.url_for(slug)}): index of this dataset.")
    lines += [
        "",
        "## Other",
        "",
        f"- [Full dataset index]({cfg.url_for('index.html')}): every page on the site.",
        f"- [RSS feed]({cfg.url_for('feed.xml')}): newest 200 pages.",
        f"- [Sitemap]({cfg.url_for('sitemap.xml')}): every URL.",
        "",
        "## Optional",
        "",
        f"- [RSS feed]({cfg.url_for('feed.xml')})",
        f"- [Sitemap]({cfg.url_for('sitemap.xml')})",
        "",
    ]
    return "\n".join(lines)

def not_found_html(cfg: Config, theme: dict[str, Any], css: str, stamp: str) -> str:
    """A real 404 document.

    Without this file the static host answers every unknown path with the homepage and
    a 200 status. That is a soft 404: it hands search engines an unbounded number of URLs
    that all carry identical content, which wastes crawl budget and suppresses indexing
    of the real pages. Cloudflare Pages and GitHub Pages both serve 404.html with a
    genuine 404 status.
    """
    home = _esc(cfg.url_for("index.html"))
    sitemap = _esc(cfg.url_for("sitemap.xml"))
    return f"""<!DOCTYPE html>
<html lang="{cfg.language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found | {_esc(cfg.site_name)}</title>
<meta name="robots" content="noindex, follow">
<meta name="description" content="That page does not exist on {_esc(cfg.site_name)}.">
<link rel="canonical" href="{home}">
<style>
{css}
.nf {{ max-width: 640px; margin: 0 auto; padding: 48px 20px; }}
.nf h1 {{ font-size: 1.7rem; margin: 0 0 10px; }}
.nf p {{ color: #4a5568; margin: 0 0 18px; }}
.nf a {{ color: var(--accent); }}
</style>
</head>
<body class="{theme['page']}-body">
<main class="{theme['page']}">
<div class="nf">
<h1>That page is not here</h1>
<p>The address you followed does not match any dataset page. Every figure on this site lives
under one of the hubs below, and the full list is in the sitemap.</p>
<p><a href="{home}">All datasets</a> &middot; <a href="{sitemap}">Sitemap</a></p>
<p style="font-size:.85rem;color:#718096">Last build: {_esc(stamp)}</p>
</div>
</main>
</body>
</html>
"""


def rss_date(stamp: str) -> str:
    """Convert an ISO-8601 stamp into the RFC 822 form RSS 2.0 requires.

    RSS ``pubDate`` and ``lastBuildDate`` are RFC 822, not ISO 8601: ``Sat, 03 Oct 2026
    21:41:45 GMT``. We were emitting the raw ISO stamp, so a parser rejects the whole
    channel-level date and reports the feed as broken rather than showing an item date.
    Feedrabbit returned exactly that, "Only UTC times are supported", for a feed whose
    timestamp was already UTC in a form it could not read. An ISO string with a ``+00:00``
    offset is valid Atom, not valid RSS.
    """
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def rss_feed(pages: list[Page], cfg: Config, stamp: str, limit: int = 200) -> str:
    """RSS 2.0 feed of the most recently generated pages.

    A sitemap tells crawlers what exists; a feed is what aggregators and readers subscribe
    to, and it costs one file. The full corpus is too large to send, so this carries the
    newest slice.
    """
    stamp = rss_date(stamp or datetime.now(timezone.utc).isoformat())
    from xml.sax.saxutils import escape as xml_escape

    items = []
    for page in pages[:limit]:
        url = cfg.url_for(page.path)
        items.append(
            f"    <item>\n"
            f"      <title>{xml_escape(page.title)}</title>\n"
            f"      <link>{xml_escape(url)}</link>\n"
            f"      <guid isPermaLink=\"true\">{xml_escape(url)}</guid>\n"
            f"      <description>{xml_escape(page.summary[:280])}</description>\n"
            f"      <pubDate>{stamp}</pubDate>\n"
            f"    </item>"
        )
    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{xml_escape(cfg.site_name)}</title>
    <link>{xml_escape(cfg.domain)}/</link>
    <description>Reference pages rebuilt daily from public data APIs.</description>
    <lastBuildDate>{stamp}</lastBuildDate>
{body}
  </channel>
</rss>
"""


def headers_file() -> str:
    """Cloudflare Pages `_headers`, which is the only caching control this site has.

    Everything is static, so there is no Worker to rate-limit and no per-request compute
    to protect: a scrape of the API costs bandwidth, not CPU. What it does cost is an
    origin round trip per request, and Pages defaults every asset to
    `max-age=0, must-revalidate`, which forces revalidation on every single hit.

    So the useful lever here is caching, not rate limiting. The derived data files change
    at most once a day, because they are rebuilt once a day, so an hour is safe and an
    edge hit avoids the origin entirely. HTML keeps a short max-age rather than a long
    one: it changes daily and carries the ad tags, and a crawler should see the current
    build rather than a cached copy of yesterday's.
    """
    # Pages matches these patterns against the *request path*, and this site is served
    # extensionless, so a "/*.html" rule never fires: every page is requested as
    # "/top-coldest-cities". The catch-all below is therefore the rule that actually
    # applies to HTML, with the data files overridden after it.
    return """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Cache-Control: public, max-age=300, must-revalidate

/api/*
  Cache-Control: public, max-age=3600

/search-index.json
  Cache-Control: public, max-age=3600

/feed-climate.xml
  Cache-Control: public, max-age=1800

/feed-crypto.xml
  Cache-Control: public, max-age=1800

/feed-holidays.xml
  Cache-Control: public, max-age=1800

/assets/*
  Cache-Control: public, max-age=86400
"""
