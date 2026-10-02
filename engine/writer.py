"""Delta-aware output writer: only rewrites pages whose content hash changed."""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .hubs import SECTIONS
from .http import read_json_cache, write_json_cache
from .models import ManifestEntry, Page

log = logging.getLogger("pseo.writer")

MANIFEST = "manifest.json"


class Writer:
    def __init__(self, output_dir: Path, cache_dir: Path) -> None:
        self.output = Path(output_dir)
        self.cache_dir = Path(cache_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.cache_dir / MANIFEST
        manifest = read_json_cache(self.manifest_path, {}) or {}
        self.previous: dict[str, str] = {
            k: v.get("content_hash", "") for k, v in manifest.get("pages", {}).items()
        }

    def write_pages(self, pages: list[Page], render_fn, stamp: str) -> tuple[list[str], int]:
        """Render and write only new/changed pages. Returns (new_urls, written_count)."""
        new_urls: list[str] = []
        written = 0
        for page in pages:
            destination = self.output / page.path
            if self.previous.get(page.path) == page.content_hash and destination.exists():
                continue
            destination.write_text(render_fn(page), encoding="utf-8")
            written += 1
            if page.path not in self.previous:
                new_urls.append(page.path)
        log.info("wrote %d pages (%d unchanged)", written, len(pages) - written)
        return new_urls, written

    def write_raw(self, filename: str, content: str) -> None:
        (self.output / filename).write_text(content, encoding="utf-8")

    def write_deploy_metadata(self) -> None:
        """Keep static hosts from swallowing the site: no Jekyll, no underscore dirs."""
        (self.output / ".nojekyll").write_text("", encoding="utf-8")

    def sync_manifest(self, pages: list[Page], stamp: str, prune: bool = False) -> None:
        # The manifest is cumulative: each run covers a slice of the corpus, so pages
        # produced by earlier runs must stay registered or they would be forgotten and
        # their files treated as orphans.
        previous_pages = (read_json_cache(self.manifest_path, {}) or {}).get("pages", {})
        entries = dict(previous_pages)
        for page in pages:
            entries[page.path] = asdict(ManifestEntry.from_page(page, stamp))
        write_json_cache(self.manifest_path, {"updated_at": stamp, "pages": entries})

        keep = set(entries) | {"index.html", "sitemap.xml", "robots.txt"}
        keep |= {f"{meta['slug']}.html" for meta in SECTIONS.values()}
        removed = 0
        for path in self.output.glob("*.html"):
            if path.name not in keep:
                path.unlink()
                removed += 1
        if removed:
            log.info("removed %d orphan pages", removed)
        if prune:
            trimmed = {k: v for k, v in entries.items() if (Path(self.output) / k).exists()}
            if len(trimmed) != len(entries):
                write_json_cache(self.manifest_path, {"updated_at": stamp, "pages": trimmed})
                log.info("pruned %d manifest entries without files", len(entries) - len(trimmed))

    def clone_to(self, destination: Path) -> None:
        """Publish the current output directory to another branch/worktree (CI no-op)."""
        destination = Path(destination)
        if destination == self.output:
            return
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(self.output, destination)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()