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
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.config import load_config  # noqa: E402
from engine.enrich import Enricher  # noqa: E402
from engine.hubs import assign_related, hub_documents, robots_txt, sitemap_xml  # noqa: E402
from engine.http import Http  # noqa: E402
from engine.indexing import notify  # noqa: E402
from engine.models import Page  # noqa: E402
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

        assign_related(pages, cfg)

        enricher = Enricher(cfg, http)
        reserved = enricher.enrich_all(pages)
        if reserved:
            await enricher.run(pages)
        log.info("gemini stats: %s", enricher.stats)

        theme = build_theme()
        css = build_css(theme)

        def render_one(page: Page) -> str:
            prose, faq = pick_copy(page, enricher.cache)
            return render_page(page, cfg, theme, css, prose, faq, stamp)

        new_files, written = writer.write_pages(pages, render_one, stamp)

        for filename, html_doc in hub_documents(pages, cfg, theme, css):
            writer.write_raw(filename, html_doc)
        if cfg.seo.get("generate_sitemap", True):
            writer.write_raw("sitemap.xml", sitemap_xml(pages, cfg, stamp))
            writer.write_raw("robots.txt", robots_txt(cfg))
        writer.sync_manifest(pages, stamp, prune=args.prune)

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