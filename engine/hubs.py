"""Internal linking, hub pages and sitemap generation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .config import Config
from .models import Link, Page
from .render import render_hub

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


def assign_related(pages: list[Page], cfg: Config) -> None:
    """Link every page to neighbours of the same kind plus its section hub."""
    by_kind: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_kind[page.kind].append(page)

    for page in pages:
        section = SECTIONS.get(page.kind)
        links: list[Link] = []
        if section:
            links.append(Link(f"All: {section['h1']}", cfg.url_for(f"{section['slug']}.html")))
        siblings = [p for p in by_kind[page.kind] if p.slug != page.slug]
        random_offset = abs(hash(page.slug)) % max(1, len(siblings))
        rotated = siblings[random_offset:] + siblings[:random_offset]
        for sib in rotated[:RELATED_PER_PAGE]:
            links.append(Link(sib.h1, cfg.url_for(sib.path)))
        page.related = links


def hub_documents(
    pages: list[Page], cfg: Config, theme: dict, css: str
) -> list[tuple[str, str]]:
    """Return [(filename, html)] for the root index plus one hub per section."""
    stamp = datetime.now(timezone.utc).isoformat()
    docs: list[tuple[str, str]] = []
    by_kind: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_kind[page.kind].append(page)

    all_links = [
        Link(page.h1, cfg.url_for(page.path))
        for page in sorted(pages, key=lambda p: (p.kind, p.h1))
    ][:600]
    index_html = render_hub(
        title=f"{cfg.site_name} - free public data reference",
        h1=f"{cfg.site_name}: {len(pages)} automatically updated data pages",
        intro=(
            f"This site publishes {len(pages)} data pages built from public APIs on a daily "
            "schedule. Every value is sourced, dated and linked to neighbouring datasets so you can "
            "compare years, cities or countries without digging through spreadsheets."
        ),
        links=all_links,
        cfg=cfg,
        theme=theme,
        css=css,
        stamp=stamp,
    )
    docs.append(("index.html", index_html))

    for kind, meta in SECTIONS.items():
        items = by_kind.get(kind, [])
        if not items:
            continue
        links = [
            Link(p.h1, cfg.url_for(p.path))
            for p in sorted(items, key=lambda p: p.h1)[:600]
        ]
        html_doc = render_hub(
            title=f"{meta['h1']} | {cfg.site_name}",
            h1=meta["h1"],
            intro=(
                f"{len(items)} pages in this collection. Each page carries its own summary, key "
                "figures and FAQ, and links to the closest matching datasets."
            ),
            links=links,
            cfg=cfg,
            theme=theme,
            css=css,
            stamp=stamp,
        )
        docs.append((f"{meta['slug']}.html", html_doc))
    return docs


def sitemap_xml(pages: list[Page], cfg: Config, stamp: str) -> str:
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    urls = [("index.html", "1.0", "daily")] + [
        (f"{SECTIONS[k]['slug']}.html", "0.8", "daily") for k in SECTIONS if any(p.kind == k for p in pages)
    ]
    urls += [(p.path, "0.7", "weekly") for p in pages]
    from xml.sax.saxutils import escape as xml_escape

    entries = "\n".join(
        f"  <url><loc>{xml_escape(cfg.url_for(path))}</loc>"
        f"<lastmod>{stamp[:10]}</lastmod>"
        f"<changefreq>{freq}</changefreq>"
        f"<priority>{priority}</priority></url>"
        for path, priority, freq in urls
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{entries}
</urlset>
"""  # noqa: E501 - urls are xml-escaped above


def robots_txt(cfg: Config) -> str:
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {cfg.url_for('sitemap.xml')}\n"
    )