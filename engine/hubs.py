"""Internal linking, hub pages, pagination and sitemap generation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from typing import Any

from .config import Config
from .models import Link, Page
from .render import _esc, render_hub

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


def assign_related(pages: list[Page], cfg: Config) -> None:
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
            by_entity[entity].append(page)

    for page in pages:
        section = SECTIONS.get(page.kind)
        links: list[Link] = []
        if section:
            links.append(Link(f"All: {section['h1']}", cfg.url_for(f"{section['slug']}.html")))
        entity = str(page.data.get("entity") or "").strip()
        cross = [p for p in by_entity.get(entity, []) if p.slug != page.slug and p.kind != page.kind]
        for other in cross[:CROSS_ENTITY_LINKS]:
            links.append(Link(other.h1, cfg.url_for(other.path)))
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
    pages: list[Page], cfg: Config, theme: dict, css: str
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


def robots_txt(cfg: Config) -> str:
    return (
        "User-agent: *\nAllow: /\n"
        f"Sitemap: {cfg.url_for('sitemap.xml')}\n"
        f"# RSS feed: {cfg.url_for('feed.xml')}\n"
    )

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


def rss_feed(pages: list[Page], cfg: Config, stamp: str, limit: int = 200) -> str:
    """RSS 2.0 feed of the most recently generated pages.

    A sitemap tells crawlers what exists; a feed is what aggregators and readers subscribe
    to, and it costs one file. The full corpus is too large to send, so this carries the
    newest slice.
    """
    stamp = stamp or datetime.now(timezone.utc).isoformat()
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
