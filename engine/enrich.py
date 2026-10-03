"""Gemini enrichment: async, rate-limited, cached, with a deterministic offline fallback."""
from __future__ import annotations

import asyncio
import html
import hashlib
import logging
import re
import time
from pathlib import Path

from .config import Config, env_secret
from .http import Http, read_json_cache, write_json_cache
from .models import Page
from .ratelimit import CallBudget, RateLimiter

log = logging.getLogger("pseo.enrich")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
ENDPOINT = f"{API_ROOT}/models/{{model}}:generateContent"
MODELS_ENDPOINT = f"{API_ROOT}/models"
# Every provider below speaks the OpenAI chat-completions dialect, so one client covers
# all of them and adding another is a registry entry rather than another 80 lines of
# near-identical request, quota and model-discovery code.
#
# Order matters: ChatProvider list is walked top to bottom and the first one that
# returns text wins. Higher free throughput goes first. per_minute is deliberately well
# under each vendor's documented free-tier ceiling, because a 429 burns the whole day
# (see ChatProvider.note_quota).
OPENAI_COMPAT_PROVIDERS: dict[str, dict] = {
    "cerebras": {
        "endpoint": "https://api.cerebras.ai/v1/chat/completions",
        "models_endpoint": "https://api.cerebras.ai/v1/models",
        "key_env": "CEREBRAS_API_KEY",
        "models": ("llama-3.3-70b", "llama3.1-8b"),
        "per_minute": 25.0,
        "label": "Cerebras",
    },
    "mistral": {
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "models_endpoint": "https://api.mistral.ai/v1/models",
        "key_env": "MISTRAL_API_KEY",
        "models": ("mistral-small-latest", "open-mistral-nemo"),
        "per_minute": 20.0,
        "label": "Mistral",
    },
    "groq": {
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "models_endpoint": "https://api.groq.com/openai/v1/models",
        "key_env": "GROQ_API_KEY",
        "models": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        "per_minute": 25.0,
        "label": "Groq",
    },
}
# Kept for callers that only care about Groq.
GROQ_FALLBACK_MODELS = OPENAI_COMPAT_PROVIDERS["groq"]["models"]
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

def _key_pool(raw: str) -> list[str]:
    """Accept comma, space or newline separated keys from one secret.

    Rotating across keys multiplies the free-tier quota, so a single exhausted key
    must not stop enrichment for the whole run.
    """
    parts = [p.strip() for p in raw.replace("\n", ",").replace(" ", ",").split(",")]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return seen


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


class ChatProvider:
    """One OpenAI-compatible chat endpoint: key, rate limit, daily quota, model discovery.

    Kept deliberately generic so Cerebras, Mistral and Groq share one implementation.
    The two behaviours that actually cost us time live here:

    * A 429 or a daily-cap message is latched to the UTC day and never retried, because
      retrying a capped key just burns wall clock and every page then falls back.
    * Model ids are discovered per run and a 404 evicts that id for the rest of the run.
      Hardcoding a model name works until the vendor retires it, which silently costs a
      whole provider's throughput.
    """

    def __init__(self, name: str, spec: dict, cache_dir, today: str) -> None:
        self.name = name
        self.spec = spec
        self.label = spec.get("label", name)
        self.endpoint = spec["endpoint"]
        self.models_endpoint = spec["models_endpoint"]
        self.key = env_secret(spec.get("key_env", ""))
        self.model = spec.get("model") or spec["models"][0]
        self.models: list[str] = [self.model]
        self.limiter = RateLimiter(per_minute=float(spec.get("per_minute", 20.0)))
        self.quota_path = Path(cache_dir) / f"{name}_quota.json"
        self.quota = read_json_cache(self.quota_path, {}) or {}
        self.today = today
        self.http = None  # bound by bind()

    def bind(self, http) -> None:
        self.http = http

    @property
    def available(self) -> bool:
        return bool(self.key)

    @property
    def exhausted(self) -> bool:
        return bool(self.quota.get(self.today))

    def note_quota(self) -> None:
        self.quota[self.today] = True
        write_json_cache(self.quota_path, self.quota)
        log.error("%s rate/quota hit for today; skipping it for the rest of the UTC day", self.label)

    def candidates(self) -> list[str]:
        seen: list[str] = []
        for candidate in [self.model, *self.models, *self.spec.get("models", ())]:
            if candidate and candidate not in seen:
                seen.append(candidate)
        return seen

    async def call(self, page: Page, prompt: str) -> str | None:
        if not self.available:
            return None
        if self.exhausted:
            log.warning("%s is quota-exhausted today; skipping it", self.label)
            return None
        if self.http is None:
            log.error(
                "%s has no HTTP client bound; it cannot be called", self.label
            )
            return None
        await self.resolve_model()
        for model in self.candidates():
            text = await self._attempt(page, prompt, model)
            if text:
                return text
            if self.exhausted:
                return None
        return None

    async def _attempt(self, page: Page, prompt: str, model: str) -> str | None:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 700,
        }
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        for attempt in range(2):
            await self.limiter.acquire()
            try:
                data = await self.http.post_json(self.endpoint, payload, headers=headers)
                choices = data.get("choices") or []
                text = (choices[0].get("message", {}).get("content") or "").strip()
                cleaned = clean(text)
                if cleaned:
                    return cleaned
                log.warning("%s %s returned no text for %s", self.label, model, page.slug)
                return None
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                if any(marker in detail for marker in QUOTA_MARKERS) or "429" in detail:
                    self.note_quota()
                    return None
                if "404" in detail:
                    log.warning("%s model %s unavailable; trying the next candidate", self.label, model)
                    if model in self.models:
                        self.models.remove(model)
                    return None
                log.warning("%s failed (attempt %s): %s", self.label, attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(4)
        return None

    async def resolve_model(self) -> str:
        """Ask the vendor which model ids this key can call, best first."""
        if not self.available or self.http is None:
            return self.model
        try:
            data = await self.http.get_json(self.models_endpoint, headers={"Authorization": f"Bearer {self.key}"})
        except Exception as exc:  # noqa: BLE001
            log.warning("%s model discovery failed (%s); trying %s", self.label, exc, self.model)
            return self.model
        names = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
        if not names:
            log.warning("%s returned no model list; trying %s", self.label, self.model)
            return self.model

        def rank(name: str) -> tuple:
            low = name.lower()
            preferred = (
                "small" in low
                or "nemo" in low
                or "flash" in low
                or "mini" in low
                or "8b" in low
                or "instant" in low
            )
            size = next((int(p) for p in re.findall(r"(\d+)b", low)), 0)
            return (0 if preferred else 1, -size, low)

        ordered = sorted(names, key=rank)
        if ordered[0] != self.model:
            log.info("%s: using discovered model %s", self.label, ordered[0])
        self.model = ordered[0]
        self.models = ordered
        return self.model


class Enricher:
    def __init__(self, cfg: Config, http: Http) -> None:
        self.cfg = cfg
        self.http = http
        gemini_cfg = cfg.raw.get("gemini", {})
        # GEMINI_API_KEYS (any separator) takes precedence; GEMINI_API_KEY still works.
        pool = env_secret(gemini_cfg.get("keys_env", "GEMINI_API_KEYS")) or env_secret(
            gemini_cfg.get("api_key_env", "GEMINI_API_KEY")
        )
        self.keys = _key_pool(pool) if pool else []
        # OpenAI-compatible providers, in the order they should be tried. config names the
        # order so adding a paid key needs no code change. groq_key_env/groq_model stay
        # honoured so an existing config keeps working untouched.
        today = time.strftime("%Y-%m-%d")
        order = gemini_cfg.get("providers") or ["cerebras", "mistral", "groq"]
        self.chat_providers: list[ChatProvider] = []
        for pname in order:
            spec = dict(OPENAI_COMPAT_PROVIDERS.get(pname, {}))
            if not spec:
                log.warning("unknown enrichment provider %r; ignoring", pname)
                continue
            overrides = (gemini_cfg.get("provider_overrides") or {}).get(pname) or {}
            spec.update(overrides)
            if pname == "groq":
                if gemini_cfg.get("groq_key_env"):
                    spec["key_env"] = gemini_cfg["groq_key_env"]
                if gemini_cfg.get("groq_model"):
                    spec["model"] = gemini_cfg["groq_model"]
            provider = ChatProvider(pname, spec, cfg.paths.cache, today)
            # A provider without a client returns None from call(), which looks exactly
            # like an exhausted quota: the run silently drops to fallback copy with no
            # error anywhere. Bind here so a missing bind fails loudly instead.
            if http is not None:
                provider.bind(http)
            else:
                log.warning("enrichment provider %s constructed without an HTTP client", pname)
            self.chat_providers.append(provider)
        # Back-compat attributes: tests and callers still ask whether Groq is present.
        self.groq_key = next((p.key for p in self.chat_providers if p.name == "groq"), None)
        self.groq_model = next((p.model for p in self.chat_providers if p.name == "groq"), GROQ_FALLBACK_MODELS[0])
        self.api_key = self.keys[0] if self.keys else None
        # Keys that reported a daily quota exhaustion, remembered for the UTC day.
        self.quota_path = cfg.paths.cache / "gemini_quota.json"
        self.quota = read_json_cache(self.quota_path, {}) or {}
        self._today = time.strftime("%Y-%m-%d")
        limits = cfg.limits
        self.model = cfg.raw.get("gemini", {}).get("model") or FALLBACK_MODELS[0]
        self.limiter = RateLimiter(per_minute=60.0 / float(limits.get("gemini_min_interval_seconds", 4.1)))
        self.budget = CallBudget(int(limits.get("max_gemini_calls_per_run", 100)))
        self.enabled = bool(gemini_cfg.get("enabled", True)) and bool(
            self.keys or any(p.available for p in self.chat_providers)
        )
        self.cache_path = cfg.paths.cache / "gemini_cache.json"
        self.model_cache_path = cfg.paths.cache / "gemini_model.json"
        self.cache: dict[str, str] = read_json_cache(self.cache_path, {}) or {}
        self.stats = {"hit": 0, "generated": 0, "fallback": 0, "failed": 0}
        # Consecutive hard failures (bad key, wrong model, quota exhausted) disable
        # enrichment for the rest of the run instead of retrying every page.
        self.consecutive_failures = 0
        self._gemini_skip_logged = False
        self.hard_failure_cutoff = 3
        self.rate_limited = 0
        self.rate_limit_cutoff = 2

    @property
    def gemini_available(self) -> bool:
        """True when some Gemini key is still usable today.

        A daily quota exhaustion is remembered per key for the UTC day. With a single
        key configured, the old guard logged "using the next one" and then carried on
        calling the same exhausted key, once per candidate model, each with its own
        HTTP retries and exponential backoff. That is minutes of a cron job waiting on
        an error that cannot recover until tomorrow.
        """
        if not self.keys:
            return False
        exhausted = self.quota.get(self._today)
        return not exhausted or any(key != exhausted for key in self.keys)

    async def resolve_model(self) -> str:
        """Find a model this key can actually call.

        Hardcoding a model name breaks the moment Google retires or re-scopes it:
        gemini-1.5-flash already returns 404, and a discovery list that mixes text
        and speech models will happily pick a TTS endpoint that returns no prose.
        So ask the API every run (one cheap call), keep only models that support
        generateContent and produce text, and cache the choice for logging only.
        """
        cached = (read_json_cache(self.model_cache_path, {}) or {}).get("model")
        if not self.gemini_available:
            log.warning(
                "every gemini key is quota-exhausted today; skipping model discovery"
            )
            if cached:
                self.model = cached
            return self.model
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
        for provider in self.chat_providers:
            text = await provider.call(page, prompt)
            if text:
                self.stats["provider"] = provider.label
                return text
        if not self.keys:
            return None
        if not self.gemini_available:
            if not self._gemini_skip_logged:
                log.warning("every gemini key is quota-exhausted today; using fallback copy")
                self._gemini_skip_logged = True
            return None
        models = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
        start_key = self.api_key
        for model in models:
            text = await self._call_model(page, prompt, model)
            if self.api_key != start_key:
                models = [model] + [m for m in FALLBACK_MODELS if m != model]
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
                if any(marker in str(exc) for marker in QUOTA_MARKERS):
                    self._mark_exhausted(model)
                    return None
                if RATE_LIMIT_MARKER in str(exc):
                    # Quota pacing, not a broken key: slow down instead of giving up.
                    self.rate_limited += 1
                    if self.rate_limited >= self.rate_limit_cutoff:
                        log.warning("rate limited %s times; halving the request pace", self.rate_limited)
                        self.limiter.min_interval *= 2
                        self.rate_limited = 0
                    log.warning("gemini rate limited; next call in a slower window")
                    return None
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

    # --- Groq back-compat shims -------------------------------------------------
    # Older config and tests still speak in Groq terms. Groq is now just the first
    # member of the provider list, so these forward to it instead of owning logic.
    @property
    def _groq(self) -> "ChatProvider | None":
        return next((p for p in self.chat_providers if p.name == "groq"), None)

    @property
    def groq_model(self) -> str:
        provider = self._groq
        return provider.model if provider else GROQ_FALLBACK_MODELS[0]

    @groq_model.setter
    def groq_model(self, value: str) -> None:
        provider = self._groq
        if provider:
            provider.model = value

    @property
    def groq_models(self) -> list[str]:
        provider = self._groq
        return provider.models if provider else []

    @groq_models.setter
    def groq_models(self, value: list[str]) -> None:
        provider = self._groq
        if provider:
            provider.models = value

    def groq_candidates(self) -> list[str]:
        provider = self._groq
        return provider.candidates() if provider else []

    def _mark_exhausted(self, model: str) -> str | None:
        """Record the exhausted key and move to the next one, or stop if none left."""
        if self.api_key:
            self.quota[self._today] = self.api_key
            write_json_cache(self.quota_path, self.quota)
        remaining = [k for k in self.keys if k != self.api_key]
        if remaining:
            log.error(
                "gemini quota exhausted on one key; rotating to the next of %d", len(remaining)
            )
            self.keys = remaining
            self.api_key = remaining[0]
            return self.api_key
        log.error(
            "gemini quota exhausted on every key; keeping cached or fallback copy for the "
            "remaining pages"
        )
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
RATE_LIMIT_MARKER = "HTTP 429"
# A per-minute cap says "retry shortly". An exhausted daily free-tier quota says this
# and will not recover during the run, so stop instead of burning the job timeout.
QUOTA_MARKERS = ("exceeded your current quota", "quota exceeded", "RESOURCE_EXHAUSTED")


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


def _num(text: str) -> float | None:
    """First number in a fact value such as 'Jul (20.5 C)' or '602 mm / year'."""
    match = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(match.group()) if match else None


def _month_insight(rows: list[Any]) -> dict[str, Any] | None:
    """Derive comparison sentences from a monthly climate table.

    The prose has to say something the table does not, or it is padding. These give each
    city page a distinct, checkable claim: how far the warmest month sits above the
    coldest, whether rain falls all year or in one season, and how the wet month compares
    with the dry one.
    """
    parsed = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            parsed.append((str(row[0]), float(row[1]), float(row[2])))
        except (TypeError, ValueError):
            continue
    if len(parsed) < 6:
        return None
    warmest = max(parsed, key=lambda r: r[1])
    coldest = min(parsed, key=lambda r: r[1])
    wettest = max(parsed, key=lambda r: r[2])
    driest = min(parsed, key=lambda r: r[2])
    total_rain = sum(r[2] for r in parsed) * 30.4
    wettest_share = wettest[2] / sum(r[2] for r in parsed) if sum(r[2] for r in parsed) else 0.0
    return {
        "warmest": warmest,
        "coldest": coldest,
        "wettest": wettest,
        "driest": driest,
        "swing": warmest[1] - coldest[1],
        "total_rain": total_rain,
        "wettest_share": wettest_share,
        "seasonal": wettest_share > 0.18,
        "rain_swing": wettest[2] / driest[2] if driest[2] else 0.0,
    }


_OPENERS = (
    "This page is a single place to check {subject}.",
    "Everything below is collected in one spot so you do not have to cross-check {subject} by hand.",
    "A consolidated reference for {subject}, rebuilt from public data on a schedule.",
    "If you only need {subject}, the figures and table here are the whole story.",
    "This is the short version of {subject}, with the monthly detail underneath.",
    "One page for {subject}, sourced from public APIs and refreshed automatically.",
)

_BRIDGES = (
    "The build runs on a schedule and caches the last good response, so the numbers stay put even when the upstream API is having a bad day.",
    "Every run re-reads the source and only rewrites this page when a figure actually changed.",
    "Values come from a public open API; a successful result is cached so a later outage never blanks the page.",
    "The same build that regenerates the sitemap refreshes these values, and unchanged pages are left untouched.",
    "Because each page is rebuilt independently, one unavailable upstream never takes the rest of the site down.",
)

_CLOSERS = (
    "Use the table for exact numbers, or the related pages below for neighbouring locations and periods.",
    "The table underneath carries the per-month breakdown; the related pages cover the same dataset nearby.",
    "Scroll for the month-by-month figures, or jump to a neighbouring location from the links below.",
    "Exact monthly values are in the table; links below cover adjacent locations and years.",
)

_CLIMATE_FRAMES = (
    "{place} runs from {coldest} in {coldest_month} to {warmest} in {warmest_month}, a swing of {swing} C.",
    "The gap between {coldest_month} and {warmest_month} is {swing} C here: {coldest} against {warmest}.",
    "Expect {coldest} at the coldest ({coldest_month}) and {warmest} at the warmest ({warmest_month}) — a {swing} C spread.",
)

_RAIN_FRAMES_SEASONAL = (
    "Rain is strongly seasonal: {wettest_month} is the wettest month at {wettest_rain} mm/day, while {driest_month} runs at {driest_rain} mm/day, about {rain_ratio}x drier.",
    "Precipitation peaks in {wettest_month} ({wettest_rain} mm/day) and bottoms out in {driest_month} ({driest_rain} mm/day), so a {rain_ratio}x difference between the two.",
    "Expect a wet season rather than rain year-round: {wettest_month} brings {wettest_rain} mm/day against {driest_rain} mm/day in {driest_month}.",
)

_RAIN_FRAMES_EVEN = (
    "Rain is spread through the year, with {wettest_month} the wettest month at {wettest_rain} mm/day and {driest_month} the driest at {driest_rain} mm/day.",
    "No month dominates: the wettest is {wettest_month} at {wettest_rain} mm/day, the driest {driest_month} at {driest_rain} mm/day.",
    "Expect a wet month and a dry month but no real season: {wettest_rain} mm/day in {wettest_month} against {driest_rain} mm/day in {driest_month}.",
)

_CLIMATE_FAQ = (
    ("Is {place} climate data a forecast?", "No. These are multi-decadal averages for the grid cell, not a prediction for any particular year."),
    ("Why do the numbers differ from a weather app?", "An app shows one location on one day. These are long-run averages, so they smooth out single hot or cold spells."),
    ("How much rain is {wettest_rain} mm/day in a month?", "Rain is recorded as a daily mean, so {wettest_rain} mm/day is roughly {monthly} mm across a 30-day month."),
    ("Does {place} get the same every year?", "Close to it. Averaging decades smooths variation, so treat these as typical rather than guaranteed."),
)


def fallback_copy(page: Page) -> tuple[str, list[tuple[str, str]]]:
    """Deterministic copy so the site still publishes without any AI key.

    The AI provider is usually rate-limited on a free tier, which means most pages ship
    this text. One template reused across every page is thin content: hundreds of pages
    differing only in a name is the pattern a search engine discounts.

    So the copy is built from the page's own figures. Each opening frame is chosen by a
    stable hash of the slug, so the phrasing varies across the corpus but never flickers
    between builds, and every page states a comparison its reader would otherwise have to
    work out from the table.
    """
    rows = ((page.data.get("table") or {}).get("rows")) or []
    insight = _month_insight(rows) if page.kind.startswith("climate") else None
    # Keep proper nouns capitalised: "berlin climate" reads like a typo.
    subject = page.h1 if page.kind.startswith("climate") else page.h1.lower()
    pick = lambda options: options[int(hashlib.sha256(page.slug.encode()).hexdigest(), 16) % len(options)]

    opener = pick(_OPENERS).format(subject=subject)
    bridge = pick(_BRIDGES)
    closer = pick(_CLOSERS)
    parts = [opener]

    if insight:
        place = page.h1.split()[0]
        wm, cm = insight["warmest"], insight["coldest"]
        wet, dry = insight["wettest"], insight["driest"]
        parts.append(
            pick(_CLIMATE_FRAMES).format(
                place=place,
                coldest=f"{cm[1]:.1f} C",
                coldest_month=cm[0],
                warmest=f"{wm[1]:.1f} C",
                warmest_month=wm[0],
                swing=f"{insight['swing']:.1f}",
            )
        )
        rain_frame = pick(_RAIN_FRAMES_SEASONAL if insight["seasonal"] else _RAIN_FRAMES_EVEN)
        parts.append(
            rain_frame.format(
                wettest_month=wet[0],
                wettest_rain=f"{wet[2]:.2f}",
                driest_month=dry[0],
                driest_rain=f"{dry[2]:.2f}",
                rain_ratio=f"{insight['rain_swing']:.1f}" if insight["rain_swing"] else "2",
            )
        )
        parts.append(
            f"Taken together that is roughly {insight['total_rain']:,.0f} mm of rain a year for the grid cell."
        )
    else:
        facts = "; ".join(f"{k.lower()} {v}" for k, v in page.facts[:4])
        if facts:
            parts.append(f"The headline figures are {facts}.")
    parts.extend((bridge, closer))
    prose = " ".join(parts)

    if insight:
        place = page.h1.split()[0]
        fill = {
            "place": place,
            "wettest_rain": f"{insight['wettest'][2]:.2f}",
            "monthly": f"{insight['wettest'][2] * 30.4:.0f}",
        }
        chosen = _CLIMATE_FAQ[
            int(hashlib.sha256((page.slug + "faq").encode()).hexdigest(), 16) % len(_CLIMATE_FAQ) :
        ][:2]
        faq = [(q.format(**fill), a.format(**fill)) for q, a in chosen]
    else:
        faq = [
            (
                f"Where does the data on {subject} come from?",
                "A public open API, queried automatically and cached after every successful run.",
            ),
            (
                f"How often is {subject} updated?",
                "The build runs on a schedule, so values refresh at least daily and older pages stay live.",
            ),
        ]
    return prose, faq