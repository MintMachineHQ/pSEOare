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

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
ENDPOINT = f"{API_ROOT}/models/{{model}}:generateContent"
MODELS_ENDPOINT = f"{API_ROOT}/models"
# Tried in order when model discovery is unavailable.
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash")
# Model ids that answer generateContent but are useless for prose: speech, image,
# transcription, embedding and computer-use endpoints. Ranking must skip them or it
# happily picks a TTS model and every page comes back empty.
NON_TEXT_MARKERS = (
    "tts",
    "image",
    "transcribe",
    "omni",
    "robotics",
    "computer-use",
    "nano-banana",
    "lyria",
    "embedding",
    "vision",
    "gemma",
)

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
        self.model = cfg.raw.get("gemini", {}).get("model") or FALLBACK_MODELS[0]
        self.limiter = RateLimiter(per_minute=60.0 / float(limits.get("gemini_min_interval_seconds", 4.1)))
        self.budget = CallBudget(int(limits.get("max_gemini_calls_per_run", 100)))
        self.enabled = bool(cfg.raw.get("gemini", {}).get("enabled", True)) and bool(self.api_key)
        self.cache_path = cfg.paths.cache / "gemini_cache.json"
        self.model_cache_path = cfg.paths.cache / "gemini_model.json"
        self.cache: dict[str, str] = read_json_cache(self.cache_path, {}) or {}
        self.stats = {"hit": 0, "generated": 0, "fallback": 0, "failed": 0}
        # Consecutive hard failures (bad key, wrong model, quota exhausted) disable
        # enrichment for the rest of the run instead of retrying every page.
        self.consecutive_failures = 0
        self.hard_failure_cutoff = 3

    async def resolve_model(self) -> str:
        """Find a model this key can actually call.

        Hardcoding a model name breaks the moment Google retires or re-scopes it:
        gemini-1.5-flash already returns 404, and a discovery list that mixes text
        and speech models will happily pick a TTS endpoint that returns no prose.
        So ask the API every run (one cheap call), keep only models that support
        generateContent and produce text, and cache the choice for logging only.
        """
        cached = (read_json_cache(self.model_cache_path, {}) or {}).get("model")

        try:
            data = await self.http.get_json(MODELS_ENDPOINT, {"key": self.api_key})
        except Exception as exc:  # noqa: BLE001
            if cached:
                log.warning("model discovery failed (%s); reusing cached %s", exc, cached)
                self.model = cached
                return self.model
            log.warning("model discovery failed (%s); trying %s", exc, self.model)
            return self.model

        candidates: list[str] = []
        for entry in (data.get("models") or []):
            name = (entry.get("name") or "").split("/")[-1]
            methods = entry.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                candidates.append(name)

        if not candidates:
            log.warning("no generateContent-capable model returned; keeping %s", self.model)
            return self.model

        text_models = [
            n for n in candidates if not any(m in n.lower() for m in NON_TEXT_MARKERS)
        ]
        # Flash before pro: prose generation is cheap and fast on flash, and the free
        # tier quota is friendlier. Prefer a stable release over a dated preview.
        pool = [n for n in text_models if "flash" in n.lower()] or text_models or candidates

        def rank(name: str) -> tuple:
            version_parts: list[float] = []
            for chunk in name.replace("-", " ").split():
                if chunk.replace(".", "").isdigit():
                    version_parts.extend(float(x) for x in chunk.split(".") if x)
            version = tuple(version_parts) or (0.0,)
            return (version, -int("preview" in name), -int("lite" in name), name)

        self.model = sorted(pool, key=rank)[-1]
        write_json_cache(
            self.model_cache_path,
            {"model": self.model, "for": self.api_key[-6:], "seen": sorted(candidates)},
        )
        log.info("gemini model resolved to %s", self.model)
        return self.model

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
        await self.resolve_model()
        log.info("enriching %d pages with %s", len(targets), self.model)

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
            if not self.enabled:
                log.warning("gemini disabled mid-run: %d pages keep fallback copy", len(targets) - start)
                break
            await asyncio.gather(*(worker(p) for p in targets[start : start + 4]))

        self.cache = {k: v for k, v in self.cache.items() if v}
        write_json_cache(self.cache_path, self.cache)

    async def _call(self, page: Page) -> str | None:
        facts = ", ".join(f"{k}: {v}" for k, v in page.facts[:6])
        prompt = PROMPT.format(title=page.h1, summary=page.summary, facts=facts)
        models = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
        for model in models:
            text = await self._call_model(page, prompt, model)
            if text:
                if model != self.model:
                    log.warning("switched to %s after %s failed", model, self.model)
                    self.model = model
                    write_json_cache(
                        self.model_cache_path,
                        {"model": model, "for": self.api_key[-6:]},
                    )
                return text
            if self.consecutive_failures >= self.hard_failure_cutoff:
                return None
        return None

    async def _call_model(self, page: Page, prompt: str, model: str) -> str | None:
        url = ENDPOINT.format(model=model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700},
        }
        for attempt in range(3):
            await self.limiter.acquire()
            try:
                data = await self.http.post_json(
                    url, payload, headers={"x-goog-api-key": self.api_key}
                )
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                cleaned = clean(text)
                if not cleaned:
                    log.warning("model %s returned no text for %s", model, page.slug)
                    return None
                self.consecutive_failures = 0
                return cleaned
            except Exception as exc:  # noqa: BLE001
                self.consecutive_failures += 1
                if _is_hard_failure(exc):
                    if "404" in str(exc):
                        # Wrong or retired model: let the caller try the next candidate.
                        log.warning("model %s unavailable (%s)", model, exc)
                        return None
                    log.error("gemini rejected the request (%s); falling back for this run", exc)
                    self._trip_cutoff()
                    return None
                wait = (2 ** attempt) * 8
                log.warning("gemini failed (attempt %s) for %s: %s", attempt + 1, page.slug, exc)
                if attempt < 2:
                    await asyncio.sleep(wait)
        if self.consecutive_failures >= self.hard_failure_cutoff:
            self._trip_cutoff()
        return None

    def _trip_cutoff(self) -> None:
        if self.enabled:
            log.warning(
                "gemini disabled for the rest of this run after %s consecutive failures",
                self.consecutive_failures,
            )
        self.enabled = False


HARD_FAILURE_MARKERS = ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "API_KEY_INVALID")


def _is_hard_failure(exc: Exception) -> bool:
    """A bad key, wrong model or exhausted quota will not fix itself on retry."""
    text = str(exc)
    return any(marker in text for marker in HARD_FAILURE_MARKERS)


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