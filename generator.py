#!/usr/bin/env python3
"""Autonomous pSEO engine.

Collects data from key-free public APIs, enriches each page with Gemini (when a
key is present), renders unique HTML, writes only changed pages, and notifies
search engines about new URLs.

Usage:
    python generator.py                 # full run
    python generator.py --dry-run       # collect + render, skip network indexing
    python generator.py --limit 20      # cap generated pages (CI smoke test)
    python generator.py --no-gemini     # deterministic copy only
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.config import load_config  # noqa: E402
from engine.enrich import Enricher  # noqa: E402
from engine.hubs import (  # noqa: E402
    assign_related,
    hub_documents,
    load_entity_index,
    not_found_html,
    robots_txt,
    headers_file,
    llms_txt,
    rss_feed,
    save_entity_index,
    sitemap_xml,
)
from engine.http import Http  # noqa: E402
from engine.indexing import notify  # noqa: E402
from engine import explore  # noqa: E402
from engine.metrics import MetricsStore, metrics_for  # noqa: E402
from engine.models import Page, set_render_signature  # noqa: E402
from engine.ratelimit import CallBudget  # noqa: E402
from engine.render import build_css, build_theme, pick_copy, render_page  # noqa: E402
from engine.sources import collect_all  # noqa: E402
from engine.writer import Writer  # noqa: E402

log = logging.getLogger("pseo")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Autonomous pSEO generator")
    ap.add_argument("--config", default=None, help="path to config.json")
    ap.add_argument("--limit", type=int, default=None, help="max pages this run")
    ap.add_argument("--dry-run", action="store_true", help="skip search-engine pings")
    ap.add_argument("--no-gemini", action="store_true", help="skip AI enrichment")
    ap.add_argument(
        "--prune",
        action="store_true",
        help="drop manifest entries whose page file no longer exists",
    )
    ap.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return ap.parse_args()


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    if args.no_gemini:
        cfg.raw.setdefault("gemini", {})["enabled"] = False
    if args.limit:
        cfg.raw.setdefault("limits", {})["max_pages_per_run"] = args.limit

    if not cfg.domain or "example" in cfg.domain:
        log.warning("config.domain is still the placeholder: %s", cfg.domain)

    stamp = datetime.now(timezone.utc).isoformat()
    # Presentation config is part of the page hash, so an ad-unit or theme change
    # rewrites every page instead of only the ones this run regenerates.
    set_render_signature(
        hashlib.sha256(
            json.dumps(
                {
                    "monetization": cfg.raw.get("monetization", {}),
                    "site": cfg.raw.get("site", {}),
                    "theme": cfg.raw.get("theme", {}),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:16]
    )
    limits = cfg.limits
    writer = Writer(cfg.paths.output, cfg.paths.cache)
    page_budget = CallBudget(int(limits.get("max_pages_per_run", 100)), seen=set(writer.previous))

    async with Http(
        concurrency=int(limits.get("http_concurrency", 8)),
        timeout=int(limits.get("http_timeout_seconds", 30)),
        retries=int(limits.get("max_http_retries", 3)),
    ) as http:
        pages: list[Page] = await collect_all(cfg, http, page_budget)
        if not pages:
            log.error("no pages produced; keeping the existing output directory untouched")
            return 2

        entity_index = load_entity_index(cfg.paths.cache)
        assign_related(pages, cfg, entity_index)

        enricher = Enricher(cfg, http)
        reserved = enricher.enrich_all(pages)
        if reserved:
            await enricher.run(pages)
        log.info("gemini stats: %s", enricher.stats)

        theme = build_theme()
        css = build_css(theme)
        copies: dict[str, tuple[str, list]] = {}
        prose_version = cfg.raw.get("seo", {}).get("prose_version", 1)

        # Resolve copy up front: the hash includes it, so pages whose AI copy arrived
        # in this run are rewritten even when their data is unchanged.
        for page in pages:
            prose, faq = pick_copy(page, enricher.cache)
            copies[page.slug] = (prose, faq)
            # prose_version is part of the hash on purpose. When the copy generator
            # changes, every already-published page still carries the old text until its
            # turn comes round again, which with a thousand pages takes weeks. Including a
            # version invalidates them all at once, and each page is then rewritten once
            # as the normal per-run slice reaches it.
            page.data["copy_hash"] = hashlib.sha256(
                json.dumps([prose, faq, prose_version], sort_keys=True).encode()
            ).hexdigest()[:16]

        def render_one(page: Page) -> str:
            prose, faq = copies[page.slug]
            return render_page(page, cfg, theme, css, prose, faq, stamp)

        new_files, written = writer.write_pages(pages, render_one, stamp)
        # Record this run's pages so later, differently-scoped runs can link across
        # datasets. Cheap, and a failure here must never fail the build.
        save_entity_index(cfg.paths.cache, pages)

        for filename, html_doc in hub_documents(pages, cfg, theme, css, explore.SEARCH_BOX):
            writer.write_raw(filename, html_doc)
        # Derived surfaces: rankings, comparisons, Today, the JSON API, widgets and the
        # charts that feed them. These are built from the metrics store rather than from
        # the pages in hand, because a run only ever collects a few hundred of ~1,700 and
        # a ranking over 300 cities is the entire point.
        explore.configure(cfg.domain, cfg.site_name, clean=cfg.url_style == "clean")
        store = MetricsStore(cfg.paths.cache)
        for page in pages:
            store.record(page, metrics_for(page))
        store.save()
        derived_docs = (
            explore.ranking_pages(store, stamp)
            + explore.comparison_pages(store, stamp)
            + [explore.today_page(store, stamp)]
            + [explore.api_docs(store, stamp)]
            + explore.widget_pages(store, stamp)
            + explore.event_pages(store, stamp)
            + [explore.embed_snippet(store, stamp)]
        )
        derived_paths: list[str] = []
        derived_titles: dict[str, str] = {}
        for filename, html_doc, kind in derived_docs:
            writer.write_raw(filename, html_doc)
            derived_paths.append(filename)
            derived_titles[filename] = kind
        for filename, payload in (
            explore.api_endpoints(store, stamp)
            + explore.entity_endpoints(store, stamp)
            + explore.search_index(store, stamp)
            + explore.topic_feeds(store, stamp)
        ):
            writer.write_raw(filename, payload)
            derived_paths.append(filename)
            derived_titles[filename] = "api"
        log.info(
            "explore: %d derived pages from %d tracked rows",
            len(derived_paths),
            len(store.rows),
        )
        if cfg.seo.get("generate_sitemap", True):
            # Nested paths such as api/climate.json contribute their directory to the
            # orphan keep set, never their filename.
            # Every ancestor directory of a nested derived path, not just the first
            # segment: api/cities/ holds hundreds of files and needs keeping as a set.
            extra_dirs = {
                "/".join(p.split("/")[:i]) + "/"
                for p in derived_paths if "/" in p
                for i in range(1, len(p.split("/")))
            }
            writer.write_raw(
                "sitemap.xml",
                sitemap_xml(
                    pages,
                    cfg,
                    stamp,
                    published=writer.manifest_pages(),
                    extra=derived_paths,
                    extra_dirs=extra_dirs,
                ),
            )
            writer.write_raw("robots.txt", robots_txt(cfg))
            writer.write_raw("llms.txt", llms_txt(cfg))
            writer.write_raw("_headers", headers_file())
            writer.write_raw("feed.xml", rss_feed(pages, cfg, stamp))
        # Without a 404 document the host answers every unknown path with the homepage
        # and a 200, which reads as an unbounded set of duplicate URLs to a crawler.
        writer.write_raw("404.html", not_found_html(cfg, theme, css, stamp))
        writer.write_deploy_metadata()
        # Search Console / ad-network verification tokens live at the site root and
        # must survive the orphan cleanup, so they are written and kept explicitly.
        for name, body in (cfg.seo.get("verification_files") or {}).items():
            writer.write_raw(name, body)
        writer.sync_manifest(
            pages,
            stamp,
            prune=args.prune,
            extra_keep=set((cfg.seo.get("verification_files") or {}).keys())
            | extra_dirs,
            derived=derived_titles,
        )

        urls = [cfg.url_for(path) for path in new_files]
        stats = {"indexnow": 0, "google": 0}
        if not args.dry_run and urls:
            stats = await notify(cfg, http, urls)

        print(
            f"[pseo] pages={len(pages)} total_published={len(writer.previous)} written={written} "
            f"new={len(new_files)} "
            f"gemini={enricher.stats} indexnow={stats['indexnow']} google={stats['google']}"
        )
        return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())