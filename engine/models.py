"""Page record shared by every data source and the renderer."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Presentation config that changes rendered markup but not page data. Set once per run
# by the generator so that toggling an ad unit or restyling the site rewrites the whole
# corpus instead of only the pages a run happens to regenerate.
_RENDER_SIGNATURE = ""


def set_render_signature(value: str) -> None:
    global _RENDER_SIGNATURE
    _RENDER_SIGNATURE = value


def render_signature(_data: Any = None) -> str:
    return _RENDER_SIGNATURE


def slugify(text: str, max_len: int = 80) -> str:
    slug = _SLUG_RE.sub("-", text.lower().strip())
    slug = slug.strip("-")[:max_len].strip("-")
    if not slug:
        return "page"
    if slug[0].isdigit():
        # Keep filenames URL-safe and readable when a title starts with a number
        # or date ("1848-revolution-memorial-day" -> "on-1848-revolution-memorial-day").
        slug = f"on-{slug}"
    return slug


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Page:
    kind: str
    title: str
    h1: str
    slug: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    breadcrumbs: list[Link] = field(default_factory=list)
    related: list[Link] = field(default_factory=list)
    schema_type: str = "WebPage"
    keywords: list[str] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{self.slug}.html"

    @property
    def content_hash(self) -> str:
        """Hash everything that can change the rendered file.

        copy_hash carries the prose, so a page whose AI copy is written days later
        (after the daily Gemini quota frees up) is treated as changed and rewritten.
        Without it, enriched copy would never reach disk.

        render_signature carries the presentation config (monetization, theme, house
        ad). Without it, switching the native banner on rewrites only the pages a run
        happens to regenerate, and the rest keep serving the old markup forever.
        """
        payload = json.dumps(
            {
                "d": self.data,
                "t": self.title,
                "s": self.summary,
                "c": self.data.get("copy_hash", ""),
                "f": self.facts,
                "r": render_signature(self.data),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["content_hash"] = self.content_hash
        return d


@dataclass
class ManifestEntry:
    path: str
    kind: str
    title: str
    content_hash: str
    generated_at: str

    @classmethod
    def from_page(cls, page: Page, generated_at: str) -> "ManifestEntry":
        return cls(
            path=page.path,
            kind=page.kind,
            title=page.title,
            content_hash=page.content_hash,
            generated_at=generated_at,
        )