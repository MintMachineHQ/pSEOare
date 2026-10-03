"""HTML rendering: per-build DOM variation, JSON-LD, tables, FAQ, ad waterfall."""
from __future__ import annotations

import html
import json
import random
import re
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
SRC_RE = re.compile(r"""<script[^>]*\bsrc=["']([^"']+)["'][^>]*>\s*</script>""", re.I)

# Hosts whose traffic must never be monetised with a popunder. Referral traffic is the
# one source we cannot buy and the one we cannot afford to lose: a visitor who clicks
# from a community thread expects an answer, and an instant popunder is exactly the
# behaviour that gets a link flagged as spam and an account banned. These are matched on
# the referrer's host, including subdomains.
DEFAULT_AD_EXEMPT_HOSTS = [
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "superuser.com",
    "serverfault.com",
    "askubuntu.com",
    "quora.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "news.ycombinator.com",
    "lobste.rs",
    "tiktok.com",
    "youtube.com",
    "pinterest.com",
    "tumblr.com",
    "medium.com",
    "discord.com",
    "wikipedia.org",
]


def ad_exempt_hosts(cfg: Config) -> list[str]:
    """Referrer hosts on which the popunder must not load."""
    configured = cfg.monetization.get("popunder_exempt_hosts")
    if configured is None:
        return list(DEFAULT_AD_EXEMPT_HOSTS)
    return [str(h).strip().lower().lstrip(".") for h in configured if str(h).strip()]


def head_ads(cfg: Config) -> str:
    """Loaders the network requires immediately before </head>.

    Adsterra's popunder snippet is documented for that exact position, and it only has
    to appear once per page: these scripts install page-wide listeners, so repeating
    them inside every ad slot can fire the popunder several times and get the account
    flagged.

    When the snippet is a plain remote loader it is wrapped in a small bootstrap instead
    of being emitted directly, so the vendor script is only injected for visitors who
    arrived without a community referrer. Emitting it raw would mean the popunder arms
    itself on exactly the traffic that would complain about it. A snippet we cannot
    parse (inline test snippet, or one carrying its own behaviour) is emitted untouched.
    """
    money = cfg.monetization
    if money.get("popunder_enabled") is False:
        return ""
    raw = (money.get("popunder_script") or "").strip()
    if not raw:
        return ""
    hosts = ad_exempt_hosts(cfg)
    match = SRC_RE.search(raw)
    if not match or not hosts:
        return raw
    src = _esc(match.group(1))
    exempt = safe_json(hosts)
    return (
        "    <script>\n"
        "    // Ads are withheld from community referral traffic: an instant popunder on a\n"
        "    // referred visit is the fastest route to a removed link and a banned account.\n"
        "    (function () {\n"
        f"      var exempt = {exempt};\n"
        "      var ref = document.referrer || '';\n"
        "      var host = '';\n"
        "      try { host = new URL(ref).hostname.toLowerCase(); } catch (e) { host = ''; }\n"
        "      if (host) {\n"
        "        for (var i = 0; i < exempt.length; i++) {\n"
        "          var e = exempt[i];\n"
        "          if (host === e || host.slice(-(e.length + 1)) === '.' + e) return;\n"
        "        }\n"
        "      }\n"
        "      var s = document.createElement('script');\n"
        f"      s.src = '{src}';\n"
        "      s.async = true;\n"
        "      s.setAttribute('data-cfasync', 'false');\n"
        "      s.referrerPolicy = 'unsafe-url';\n"
        "      document.head.appendChild(s);\n"
        "    })();\n"
        "    </script>"
    )


def global_ads(cfg: Config) -> str:
    """Scripts that must appear exactly once per page, at the end of the body."""
    return (cfg.monetization.get("social_bar_script") or "").strip()


CONTAINER_RE = re.compile(r"""<div[^>]*\bid=["'](container-[^"']+)["'][^>]*>\s*</div>""", re.I)

NATIVE_BANNER_CSS = """
.native-banner{display:block;width:100%;min-height:90px;margin:0 auto;overflow:hidden}
@media (max-width:820px){.native-banner{min-height:250px}}
""".strip()


def split_native_banner(raw: str) -> tuple[str, str] | None:
    """Split a native-banner snippet into (loader script, container id).

    Networks hand out a loader plus a bare ``<div id="container-...">``. That div has no
    height, so the ad renders at zero pixels and never earns an impression. The id is
    also fixed, so the pair must be emitted exactly once per page or the id duplicates.
    """
    match = CONTAINER_RE.search(raw)
    if not match:
        return None
    loader = (raw[: match.start()].strip() + "\n" + raw[match.end():].strip()).strip()
    return loader, match.group(1)


HOUSE_AD_CSS = """
.house-ad{display:block;border:1px solid #e2e8f0;border-left:3px solid var(--accent);
  border-radius:8px;padding:14px 16px;background:#f8fafc;color:inherit;text-decoration:none}
.house-ad:hover{border-color:var(--accent);background:#fff}
.house-ad .ha-kicker{display:block;font-size:.72rem;letter-spacing:.09em;
  text-transform:uppercase;color:#64748b;margin-bottom:4px}
.house-ad .ha-headline{display:block;font-weight:600;font-size:1.02rem;margin-bottom:3px}
.house-ad .ha-text{display:block;font-size:.88rem;color:#475569}
""".strip()


def house_ad_html(cfg: Config, theme: dict[str, Any], slot: str) -> str:
    """A first-party promo for slots no ad network is filling.

    A slot left empty earns nothing, and a slot pointing at a placeholder offer earns
    nothing either. This links to the site's own most relevant hub instead, which keeps
    the visitor moving through the corpus. Every internal click is another exit from a
    page, and every exit is another opportunity for the popunder.

    Rendered as static HTML rather than by the waterfall, so it is present without
    JavaScript and is large enough (height, width, text) that the blank-slot detector
    leaves it alone.
    """
    promo = cfg.monetization.get("house_ad") or {}
    if promo.get("enabled") is False:
        return ""
    target = (promo.get("url") or "").strip()
    if not target:
        return ""
    target = cfg.url_for(target) if not target.startswith("http") else target
    return f"""      <a class="house-ad" href="{_esc(target)}" rel="nofollow sponsored">
        <span class="ha-kicker">{_esc(promo.get("kicker", "More reference data"))}</span>
        <span class="ha-headline">{_esc(promo.get("headline", "Browse the full dataset index"))}</span>
        <span class="ha-text">{_esc(promo.get("text", "Every figure on this site links to its own source and a hub of neighbouring pages."))}</span>
      </a>"""


def ad_block(cfg: Config, theme: dict[str, Any], slot: str) -> str:
    money = cfg.monetization
    native_slot = (money.get("native_banner_slot") or "foot").strip()
    banner_on = money.get("native_banner_enabled") is not False
    parts: list[str] = []
    for key in ("adsterra_script", "monetag_script"):
        raw = (money.get(key) or "").strip()
        if not raw:
            continue
        native = split_native_banner(raw)
        if native is None:
            parts.append(f"    {raw}")
            continue
        loader, container_id = native
        if not banner_on or slot != native_slot:
            # Same unit, same container id: emitting it twice would duplicate the id and
            # split impressions. The other slots fall through to the house ad.
            # native_banner_enabled is the kill switch for networks whose creative
            # selection we cannot fully control, e.g. an unfiltered site category.
            continue
        parts.append(f"    {loader}")
        parts.append(f'    <div class="native-banner" id="{container_id}"></div>')
    body = "\n".join(parts) if parts else house_ad_html(cfg, theme, slot)
    if not body.strip():
        body = '      <span class="ad-hint">ad slot</span>'
    return f"""    <aside class="ad-zone {theme['page']}-ad-{slot}" data-ad-slot="{slot}" aria-label="Advertisement">
{body}
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
{NATIVE_BANNER_CSS}
{HOUSE_AD_CSS}
</style>
{head_ads(cfg)}
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
{global_ads(cfg)}
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
{NATIVE_BANNER_CSS}
{HOUSE_AD_CSS}
</style>
{head_ads(cfg)}
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