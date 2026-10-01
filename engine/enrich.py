"""Gemini enrichment: async, rate-limited, cached, with a deterministic offline fallback."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from .config import Config, env_secret
from .http import Http, read_json_cache, write_json_cache
from .models import Page
from .ratelimit import CallBudget, RateLimiter

log = logging.getLogger("pseo.enrich")

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You write short, factual, SEO-friendly copy for a data reference website.

Topic: {title}
Page summary (facts you must not contradict):
{summary}
Key facts: {facts}

Write in English. Requirements:
- 90-130 words of plain informative prose about this specific data page, no marketing language.
- Never invent numbers. Use only the facts above; describe the rest qualitatively.
- Start with a direct answer sentence that contains the topic phrase.
- Then exactly two FAQ items formatted as:
FAQ: <question>?|<answer under 35 words>
FAQ: <question>?|<answer under 35 words>
- Do not output HTML tags, markdown headers, or preamble."""


class Enricher:
    def __init__(self, cfg: Config, http: Http) -> None:
        self.cfg = cfg
        self.http = http
        self.api_key = env_secret(cfg.raw.get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"))
        limits = cfg.limits
        self.model = cfg.raw.get("gemini", {}).get("model", "gemini-1.5-flash")
        self.limiter = RateLimiter(per_minute=60.0 / float(limits.get("gemini_min_interval_seconds", 4.1)))
        self.budget = CallBudget(int(limits.get("max_gemini_calls_per_run", 100)))
        self.enabled = bool(cfg.raw.get("gemini", {}).get("enabled", True)) and bool(self.api_key)
        self.cache_path = cfg.paths.cache / "gemini_cache.json"
        self.cache: dict[str, str] = read_json_cache(self.cache_path, {}) or {}
        self.stats = {"hit": 0, "generated": 0, "fallback": 0, "failed": 0}

    def enrich_all(self, pages: list[Page]) -> dict[str, str]:
        """Return {cache_key: text} for the pages that need copy."""
        out: dict[str, str] = {}
        for page in pages:
            key = str(page.data.get("key") or page.slug)
            if key in self.cache:
                out[key] = self.cache[key]
                self.stats["hit"] += 1
            elif self.enabled and self.budget.take():
                self.cache[key] = ""  # reserve slot
                out[key] = ""  # signals "needs async fetch"
        return out

    async def run(self, pages: list[Page]) -> None:
        if not self.enabled:
            log.info("gemini disabled or missing key: using deterministic fallback copy")
            return
        targets = [p for p in pages if str(p.data.get("key") or p.slug) in self.cache]
        # Re-fetch only slots reserved in this run.
        targets = [p for p in targets if self.cache.get(str(p.data.get("key") or p.slug)) == ""]
        if not targets:
            return

        async def worker(page: Page) -> None:
            key = str(page.data.get("key") or page.slug)
            text = await self._call(page)
            if text:
                self.cache[key] = text
                self.stats["generated"] += 1
            else:
                self.cache.pop(key, None)
                self.stats["failed"] += 1

        for start in range(0, len(targets), 4):
            await asyncio.gather(*(worker(p) for p in targets[start : start + 4]))

        self.cache = {k: v for k, v in self.cache.items() if v}
        write_json_cache(self.cache_path, self.cache)

    async def _call(self, page: Page) -> str | None:
        facts = ", ".join(f"{k}: {v}" for k, v in page.facts[:6])
        prompt = PROMPT.format(title=page.h1, summary=page.summary, facts=facts)
        url = ENDPOINT.format(model=self.model)
        params = {"key": self.api_key}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700},
        }
        for attempt in range(3):
            await self.limiter.acquire()
            try:
                data = await self.http.post_json(url, payload, headers={"x-goog-api-key": self.api_key})
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                return clean(text)
            except Exception as exc:  # noqa: BLE001
                wait = (2 ** attempt) * 8
                log.warning("gemini failed (attempt %s) for %s: %s", attempt + 1, page.slug, exc)
                if attempt < 2:
                    await asyncio.sleep(wait)
        return None


def clean(text: str) -> str:
    """Strip fences/preamble and normalise whitespace."""
    text = re.sub(r"^```[a-z]*|```$", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"^(sure|here(?:'s| is)[^\n:]*:|certainly[^\n:]*:)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = html.unescape(text)
    return text.strip()


def prose_and_faq(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split enriched copy into (prose, faq list) for the renderer."""
    if not text:
        return "", []
    prose_lines: list[str] = []
    faq: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("faq:"):
            body = stripped[4:].strip()
            if "|" in body:
                q, a = body.split("|", 1)
                faq.append((q.strip().rstrip("?").strip() + "?", a.strip()))
            continue
        if stripped:
            prose_lines.append(stripped)
    return " ".join(prose_lines), faq


def fallback_copy(page: Page) -> tuple[str, list[tuple[str, str]]]:
    """Deterministic copy so the site still publishes without any AI key."""
    facts = "; ".join(f"{k.lower()} {v}" for k, v in page.facts[:4])
    prose = (
        f"This reference page collects {page.h1.lower()} in one place: {facts}. "
        f"Figures are refreshed automatically by a scheduled build and cached locally so the page "
        f"keeps its values even when the upstream API is temporarily unavailable. "
        f"Use the table below for exact numbers, or the related pages for the same dataset for "
        f"neighbouring periods and locations."
    )
    faq = [
        (
            f"Where does the data on {page.h1.lower()} come from?",
            "From a public open API that is queried automatically and cached after every successful run.",
        ),
        (
            f"How often is {page.h1.lower()} updated?",
            "The build runs on a schedule, so values refresh at least daily and old pages stay live.",
        ),
    ]
    return prose, faq