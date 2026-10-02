"""Configuration loading + path resolution."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("PSEO_CONFIG", ROOT / "config.json"))


@dataclass
class Paths:
    root: Path = ROOT
    cache: Path = ROOT / "cache"
    output: Path = ROOT / "output"
    assets: Path = ROOT / "assets"

    def ensure(self) -> None:
        for p in (self.cache, self.output, self.assets):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Config:
    raw: dict[str, Any]
    paths: Paths = field(default_factory=Paths)

    # -- top level -------------------------------------------------------
    @property
    def domain(self) -> str:
        return self.raw.get("domain", "").rstrip("/")

    @property
    def site_name(self) -> str:
        return self.raw.get("site_name", "Data Atlas")

    @property
    def language(self) -> str:
        return self.raw.get("language", "en")

    @property
    def limits(self) -> dict[str, Any]:
        return self.raw.get("limits", {})

    @property
    def sources(self) -> dict[str, Any]:
        return self.raw.get("sources", {})

    @property
    def indexing(self) -> dict[str, Any]:
        return self.raw.get("indexing", {})

    @property
    def monetization(self) -> dict[str, Any]:
        return self.raw.get("monetization", {})

    @property
    def seo(self) -> dict[str, Any]:
        return self.raw.get("seo", {})

    def source(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})

    def source_enabled(self, name: str) -> bool:
        return bool(self.source(name).get("enabled", False))

    @property
    def host(self) -> str:
        return self.domain.replace("https://", "").replace("http://", "")

    @property
    def url_style(self) -> str:
        # "clean" matches Cloudflare Pages, which strips .html and 308s to the
        # extensionless path. Emitting .html there would make every canonical and
        # internal link point at a redirect. "html" is correct for GitHub Pages.
        return self.seo.get("url_style", "html")

    def url_for(self, path: str) -> str:
        clean = path.lstrip("/")
        if self.url_style == "clean":
            if clean == "index.html":
                return f"{self.domain}/"
            if clean.endswith(".html"):
                clean = clean[: -len(".html")]
        return f"{self.domain}/{clean}"


def load_config(path: Path | None = None) -> Config:
    target = Path(path) if path else CONFIG_PATH
    with open(target, encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = Config(raw=raw)
    cfg.paths.ensure()
    return cfg


def env_secret(env_name: str | None) -> str | None:
    """Read a secret from env, tolerating multi-line JSON credentials."""
    if not env_name:
        return None
    value = os.getenv(env_name)
    if value is None:
        return None
    value = value.strip()
    if value.startswith("{"):
        return value.replace("\\n", "\n")
    return value or None