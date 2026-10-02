"""HTML rendering: per-build DOM variation, JSON-LD, tables, FAQ, ad waterfall."""
from __future__ import annotations

import html
import json
import random
import string
from typing import Any, Iterable

from .config import Config
from .enrich import fallback_copy, prose_and_faq
from .models import Link, Page

AD_PLACEHOLDER_RE = "{{[a-z_]+}}"


# --------------------------------------------------------------------------
# structural variation
# --------------------------------------------------------------------------
def random_token(length: int = 7) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=length))


def build_theme() -> dict[str, Any]:
    """One random theme (class names + spacing values) per build."""
    prefix = "b" + random_token(6)
    return {
        "prefix": prefix,
        "page": f"{prefix}-p{random_token(4)}",
        "card": f"{prefix}-c{random_token(4)}",
        "title": f"{prefix}-t{random_token(4)}",
        "grid": f"{prefix}-g{random_token(4)}",
        "width": random.randint(880, 1180),
        "gap": random.randint(14, 28),
        "radius": random.randint(4, 12),
        "lead_size": random.choice([17, 18, 19]),
        "accent": random.choice(["#0b5fff", "#0f766e", "#7c3aed", "#b91c1c", "#0369a1"]),
    }


def build_css(theme: dict[str, Any]) -> str:
    return f"""
    :root {{ --accent: {theme['accent']}; }}
    body {{ margin: 0; background: #fbfbfd; color: #16181d;
            font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
    .{theme['page']} {{ max-width: {theme['width']}px; margin: 0 auto; padding: {theme['gap']}px 18px 60px; }}
    .{theme['title']} {{ font-size: 1.85rem; line-height: 1.2; margin: 18px 0 6px; }}
    .{theme['card']} {{ background: #fff; border: 1px solid #e6e8ec; border-radius: {theme['radius']}px;
                       padding: {theme['gap']}px; margin: {theme['gap']}px 0; }}
    .{theme['grid']} {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
                        gap: {theme['gap']}px; }}
    .{theme['grid']} > div {{ background: #fff; border: 1px solid #e6e8ec;
                             border-radius: {theme['radius']}px; padding: 12px 14px; }}
    .lede {{ font-size: {theme['lead_size']}px; line-height: 1.65; color: #333842; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 15px; }}
    caption {{ text-align: left; font-weight: 600; padding: 6px 0; }}
    th, td {{ border: 1px solid #e6e8ec; padding: 7px 10px; text-align: left; }}
    th {{ background: #f2f4f8; }}
    nav.crumb {{ font-size: 14px; color: #5b6270; margin: 12px 0; }}
    nav.links a {{ display: inline-block; margin: 4px 12px 4px 0; color: var(--accent); }}
    details {{ border-top: 1px solid #e6e8ec; padding: 10px 0; }}
    footer {{ color: #5b6270; font-size: 14px; border-top: 1px solid #e6e8ec;
              margin-top: {theme['gap']}px; padding-top: 14px; }}
    """.strip()


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def safe_json(payload: Any) -> str:
    """Serialise for an inline <script type="application/ld+json"> block.

    Scraped or API-supplied strings can contain "</script>", which would terminate
    the block early and let page data execute as script. Escaping <, > and & keeps
    the JSON valid while making that breakout impossible.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------
def facts_html(facts: Iterable[tuple[str, str]], theme: dict[str, Any]) -> str:
    facts = list(facts)
    if not facts:
        return ""
    cells = "\n".join(
        f"      <div><strong>{_esc(k)}</strong><br>{_esc(v)}</div>" for k, v in facts
    )
    return f"""    <section class="{theme['card']} {theme['grid']} {theme['page']}-facts" aria-label="Key figures">
{cells}
    </section>"""


def table_html(table: dict | None) -> str:
    if not table or not table.get("rows"):
        return ""
    headers = "".join(f"<th scope=\"col\">{_esc(h)}</th>" for h in table.get("headers", []))
    rows = "\n".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in table["rows"]
    )
    caption = _esc(table.get("caption", ""))
    return f"""    <div class="table-wrap">
      <table>
        <caption>{caption}</caption>
        <thead><tr>{headers}</tr></thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </div>"""


def sections_html(sections: list[dict] | None) -> str:
    out: list[str] = []
    for section in sections or []:
        out.append(
            "    <h2>{h}</h2>\n    <p>{b}</p>".format(
                h=_esc(section.get("heading", "")), b=_esc(section.get("body", ""))
            )
        )
    return "\n".join(out)


def breadcrumb_html(links: list[Link]) -> str:
    if not links:
        return ""
    parts = []
    for i, link in enumerate(links):
        label = _esc(link.label)
        if i == len(links) - 1:
            parts.append(f"<span aria-current=\"page\">{label}</span>")
        else:
            parts.append(f"<a href=\"{_esc(link.url)}\">{label}</a>")
    return " / ".join(parts)


def related_html(links: list[Link]) -> str:
    if not links:
        return ""
    anchors = "\n".join(f'      <a href="{_esc(l.url)}">{_esc(l.label)}</a>' for l in links)
    return f"""    <section class="related">
      <h2>Related data pages</h2>
{anchors}
    </section>"""


def json_ld(page: Page, cfg: Config, canonical: str, stamp: str) -> str:
    graph: list[dict] = [
        {
            "@type": "WebPage",
            "name": page.title,
            "headline": page.title,
            "description": page.summary[:300],
            "url": canonical,
            "inLanguage": cfg.language,
            "dateModified": stamp,
            "isPartOf": {"@type": "WebSite", "name": cfg.site_name, "url": cfg.domain},
            "publisher": {"@type": "Organization", "name": cfg.site_name},
        }
    ]
    if page.schema_type == "Event":
        graph.append(
            {
                "@type": "Event",
                "name": page.h1,
                "startDate": next((v for k, v in page.facts if k == "Date"), None),
                "location": next((v for k, v in page.facts if k == "Country"), None),
                "description": page.summary[:300],
                "url": canonical,
            }
        )
    if page.facts:
        graph.append(
            {
                "@type": "Dataset",
                "name": page.title,
                "description": page.summary[:300],
                "url": canonical,
                "dateModified": stamp,
                "keywords": ", ".join(page.keywords),
                "creator": {"@type": "Organization", "name": cfg.site_name},
            }
        )
    if page.breadcrumbs:
        graph.append(
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "name": link.label,
                        **(
                        {"item": _esc(link.url)}
                        if link.url.startswith("http")
                        else {}
                    ),
                    }
                    for i, link in enumerate(page.breadcrumbs)
                ],
            }
        )
    return safe_json({"@context": "https://schema.org", "@graph": graph})


# --------------------------------------------------------------------------
# ads / waterfall
# --------------------------------------------------------------------------
def ad_block(cfg: Config, theme: dict[str, Any], slot: str) -> str:
    money = cfg.monetization
    scripts = []
    for key in ("adsterra_script", "monetag_script"):
        raw = (money.get(key) or "").strip()
        if raw:
            scripts.append(f"    {raw}")
    scripts_html = "\n".join(scripts)
    return f"""    <aside class="ad-zone {theme['page']}-ad-{slot}" data-ad-slot="{slot}" aria-label="Advertisement">
{scripts_html if scripts_html else '      <span class="ad-hint">ad slot</span>'}
    </aside>"""


WATERFALL_JS = """
(function () {
  var CPA_URL = %(cpa_url)s;
  var CPA_IMG = %(cpa_img)s;
  var CPA_TXT = %(cpa_txt)s;
  function isBlank(el) {
    if (!el) return true;
    var h = el.getBoundingClientRect().height;
    var w = el.getBoundingClientRect().width;
    var empty = el.textContent.trim().length < 12;
    return h < 40 || w < 120 || empty;
  }
  function fillFallback(el) {
    if (el.getAttribute("data-fallback")) return;
    el.setAttribute("data-fallback", "1");
    var a = document.createElement("a");
    a.href = CPA_URL;
    a.target = "_blank";
    a.rel = "nofollow sponsored noopener";
    var img = document.createElement("img");
    img.src = CPA_IMG;
    img.alt = CPA_TXT;
    img.loading = "lazy";
    img.style.width = "100%%";
    img.style.maxWidth = "728px";
    img.style.border = "1px solid #e6e8ec";
    a.appendChild(img);
    el.innerHTML = "";
    el.appendChild(a);
  }
  function check() {
    var zones = document.querySelectorAll("[data-ad-slot]");
    for (var i = 0; i < zones.length; i++) {
      if (isBlank(zones[i])) fillFallback(zones[i]);
    }
  }
  window.addEventListener("load", function () { setTimeout(check, 2500); });
  setTimeout(check, 6000);
})();
""".strip()


def waterfall_js(cfg: Config) -> str:
    money = cfg.monetization
    return WATERFALL_JS % {
        "cpa_url": json.dumps(money.get("cpa_fallback_url", "https://example.com/offer")),
        "cpa_img": json.dumps(money.get("cpa_fallback_image", "")),
        "cpa_txt": json.dumps(money.get("cpa_fallback_text", "Recommended offer")),
    }


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------
def render_page(
    page: Page,
    cfg: Config,
    theme: dict[str, Any],
    css: str,
    prose: str,
    faq: list[tuple[str, str]],
    stamp: str,
) -> str:
    canonical = cfg.url_for(page.path)
    table = table_html(page.data.get("table"))
    facts = facts_html(page.facts, theme)
    sections = sections_html(page.data.get("sections"))
    faq_block = ""
    if faq:
        items = "\n".join(
            f'      <details><summary>{_esc(q)}</summary><p>{_esc(a)}</p></details>' for q, a in faq
        )
        faq_block = f"""    <section class="{theme['card']} faq-block" aria-label="Frequently asked questions">
      <h2>Frequently asked questions</h2>
{items}
    </section>"""
    crumbs = f'<nav class="crumb" aria-label="Breadcrumb">{breadcrumb_html(page.breadcrumbs)}</nav>' if page.breadcrumbs else ""
    related = related_html(page.related)
    meta_desc = page.summary[:158]
    cache_badge = ""
    if page.data.get("from_cache"):
        cache_badge = ' <span class="badge">cached snapshot</span>'

    return f"""<!DOCTYPE html>
<html lang="{cfg.language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(page.title)} | {_esc(cfg.site_name)}</title>
<meta name="description" content="{_esc(meta_desc)}">
<link rel="canonical" href="{_esc(canonical)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_esc(page.title)}">
<meta property="og:description" content="{_esc(meta_desc)}">
<meta property="og:url" content="{_esc(canonical)}">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">
{json_ld(page, cfg, canonical, stamp)}
</script>
<style>
{css}
</style>
</head>
<body class="{theme['page']}-body">
<main class="{theme['page']}">
{crumbs}
<article class="{theme['card']}">
<h1 class="{theme['title']}">{_esc(page.h1)}{cache_badge}</h1>
<p class="lede">{_esc(prose or page.summary)}</p>
</article>
{facts}
{ad_block(cfg, theme, 'top')}
{table}
{sections}
{ad_block(cfg, theme, 'mid')}
{faq_block}
{related}
{ad_block(cfg, theme, 'foot')}
<footer>
<p>Data compiled automatically from public APIs. Values may change when the upstream source updates.
Last build: {stamp}. Questions about a figure? Check the related pages above.</p>
<p><a href="{_esc(cfg.url_for('index.html'))}">All datasets</a> &middot;
<a href="{_esc(cfg.url_for('sitemap.xml'))}">Sitemap</a></p>
</footer>
</main>
<script>
{waterfall_js(cfg)}
</script>
</body>
</html>
"""


def render_hub(
    title: str,
    h1: str,
    intro: str,
    links: list[Link],
    cfg: Config,
    theme: dict[str, Any],
    css: str,
    stamp: str,
    canonical: str,
    section_title: str,
    page_index: int = 1,
    total_pages: int = 1,
    extra_schema: dict | None = None,
) -> str:
    anchor_list = "\n".join(
        f'      <li><a href="{_esc(l.url)}">{_esc(l.label)}</a></li>' for l in links
    )
    canonical_url = cfg.url_for(canonical)
    schema = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": h1,
        "description": intro[:300],
        "url": canonical_url,
        "inLanguage": cfg.language,
        "dateModified": stamp,
    }
    if page_index > 1:
        schema["name"] = f"{h1} (page {page_index} of {total_pages})"
    if extra_schema:
        schema.update(extra_schema)
    schema_json = safe_json(schema)

    pager = ""
    if total_pages > 1:
        base = canonical.rsplit("-", 1)[0] if canonical.endswith(f"-{page_index}.html") else canonical
        prev_href = f"{base}-{page_index - 1}.html" if page_index > 1 else f"{base}.html"
        next_href = f"{base}-{page_index + 1}.html" if page_index < total_pages else ""
        items = [
            f'<a href="{_esc(cfg.url_for(prev_href))}">Previous</a>'
            if page_index > 1
            else "<span aria-disabled=\"true\">Previous</span>",
            f'<span>Page {page_index} of {total_pages}</span>',
            f'<a href="{_esc(cfg.url_for(next_href))}">Next</a>'
            if next_href
            else "<span aria-disabled=\"true\">Next</span>",
        ]
        pager = f'<nav class="pager" aria-label="Pagination">{" ".join(items)}</nav>'

    return f"""<!DOCTYPE html>
<html lang="{cfg.language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(intro[:158])}">
<link rel="canonical" href="{_esc(canonical_url)}">
<script type="application/ld+json">
{schema_json}
</script>
<style>
{css}
ul.hub {{ list-style: none; padding: 0; columns: 2; column-gap: 26px; }}
ul.hub li {{ margin: 0 0 6px; break-inside: avoid; }}
nav.pager {{ display: flex; gap: 14px; align-items: center; margin: 18px 0; }}
nav.pager a {{ color: var(--accent); }}
</style>
</head>
<body class="{theme['page']}-body">
<main class="{theme['page']}">
<article class="{theme['card']}">
<h1 class="{theme['title']}">{_esc(h1)}</h1>
<p class="lede">{_esc(intro)}</p>
</article>
<p><a href="{_esc(cfg.url_for('index.html'))}">Home</a> &rsaquo; {_esc(section_title)}</p>
<nav class="{theme['card']}" aria-label="{_esc(section_title)}">
<ul class="hub">
{anchor_list}
</ul>
</nav>
{pager}
<footer><p>Last build: {stamp}. Updated on a daily schedule from public data sources.</p></footer>
</main>
<script>
{waterfall_js(cfg)}
</script>
</body>
</html>
"""


def pick_copy(page: Page, enriched: dict[str, str] | None) -> tuple[str, list[tuple[str, str]]]:
    if enriched:
        key = str(page.data.get("key") or page.slug)
        if key in enriched:
            prose, faq = prose_and_faq(enriched[key])
            if prose:
                return prose, faq
    return fallback_copy(page)