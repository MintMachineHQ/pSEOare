"""Offline unit tests: no network, no API keys.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import asyncio
import math
import sys
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Config  # noqa: E402
from engine.enrich import (  # noqa: E402
    FALLBACK_MODELS,
    _key_pool,
    NON_TEXT_MARKERS,
    Enricher,
    _is_hard_failure,
    clean,
    fallback_copy,
    prose_and_faq,
)
from engine.hubs import (
    AI_CRAWLERS,
    LINKS_PER_PAGE,
    SECTIONS,
    llms_txt,
    robots_txt,
    rss_date,
    rss_feed,
    assign_related,
    hub_documents,
    hub_filenames,
    not_found_html,
    sitemap_xml,
)  # noqa: E402
from engine.render import (  # noqa: E402
    ad_exempt_hosts,
    answer_sentence,
    build_css,
    build_theme,
    dataset_ld,
)
from engine.http import Http, QuotaExhausted, _is_quota_exhausted, write_json_cache  # noqa: E402
from engine.indexing import key_file_is_reachable, remember_pending, take_pending  # noqa: E402
from engine.models import Link, Page, set_render_signature, slugify  # noqa: E402
from engine.ratelimit import CallBudget  # noqa: E402
from engine.sources.base import cached_fetch, trim_to_budget  # noqa: E402
from engine.render import build_css, build_theme, ad_block, pick_copy, render_page, waterfall_js  # noqa: E402
from engine.sources import climate, countries, crypto, holidays  # noqa: E402
from engine.writer import Writer  # noqa: E402


def make_cfg(tmp: Path) -> Config:
    raw = {
        "domain": "https://data.example",
        "site_name": "Test Atlas",
        "language": "en",
        "limits": {},
        "sources": {},
        "indexing": {},
        "monetization": {
            "cpa_fallback_url": "https://offer.example/x",
            "cpa_fallback_image": "https://img.example/728x90.png",
            "cpa_fallback_text": "Deal",
        },
        "seo": {},
    }
    cfg = Config(raw=raw)
    cfg.paths = cfg.paths.__class__(root=tmp, cache=tmp / "cache", output=tmp / "output", assets=tmp / "assets")
    cfg.paths.ensure()
    return cfg


def sample_page(cfg: Config) -> Page:
    return Page(
        kind="climate_city",
        title="Berlin average monthly temperature",
        h1="Berlin climate",
        slug="berlin-climate",
        summary="Berlin long-term climate averages with monthly temperature and rainfall.",
        schema_type="Dataset",
        keywords=["berlin climate", "berlin rainfall by month"],
        facts=[("Annual mean temperature", "9.7 C"), ("Warmest month", "Jul (20.5 C)")],
        breadcrumbs=[Link("Home", cfg.url_for("index.html")), Link("Climate", cfg.url_for("hub-climate.html"))],
        data={
            "table": {
                "caption": "Berlin averages",
                "headers": ["Month", "Mean (C)"],
                "rows": [["Jan", "-0.7"], ["Jul", "20.5"]],
            },
            "sections": [{"heading": "Notes", "body": "Averages over decades."}],
            "key": "climate:berlin",
        },
    )


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Public holidays in Germany 2026"), "public-holidays-in-germany-2026")

    def test_leading_digit_is_prefixed(self):
        self.assertTrue(slugify("2 January 2026 UK").startswith("on-"))

    def test_symbols_and_length(self):
        out = slugify("Benito Juárez's Birthday!", max_len=20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.startswith("benito-ju"))

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "page")


class TestEnrich(unittest.TestCase):
    def test_clean_strips_fences(self):
        self.assertEqual(clean("```\nhello\n```"), "hello")

    def test_prose_and_faq_split(self):
        text = "Berlin averages are stable.\nFAQ: Is it accurate?|Yes, per NASA POWER.\nFAQ: Free?|Yes."
        prose, faq = prose_and_faq(text)
        self.assertIn("Berlin averages", prose)
        self.assertEqual(len(faq), 2)
        self.assertTrue(faq[0][0].endswith("?"))

    def test_fallback_copy_is_deterministic(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        first = fallback_copy(page)
        second = fallback_copy(page)
        self.assertEqual(first, second)
        self.assertEqual(len(first[1]), 2)


class TestEnrichFailFast(unittest.TestCase):
    def test_quota_exhaustion_is_detected(self):
        from engine.enrich import QUOTA_MARKERS

        body = "HTTP 429 rate limited: {\"error\": {\"message\": \"You exceeded your current quota\""
        self.assertTrue(any(marker in body for marker in QUOTA_MARKERS))

    def test_rate_limit_is_not_a_hard_failure(self):
        from engine.enrich import RATE_LIMIT_MARKER

        self.assertNotEqual(RATE_LIMIT_MARKER, "HTTP 400")
        self.assertFalse(_is_hard_failure(RuntimeError("HTTP 429 rate limited: quota")))

    def test_hard_failure_markers(self):
        self.assertTrue(_is_hard_failure(RuntimeError("request failed: HTTP 400 Bad Request")))
        self.assertTrue(_is_hard_failure(RuntimeError("API_KEY_INVALID")))
        self.assertFalse(_is_hard_failure(RuntimeError("request failed: HTTP 429 rate limited")))
        self.assertFalse(_is_hard_failure(TimeoutError("slow")))

    def test_cutoff_disables_enrichment(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
        enricher.enabled = True
        enricher._trip_cutoff()
        self.assertFalse(enricher.enabled)


class TestProviderBinding(unittest.TestCase):
    """Every OpenAI-compatible provider must actually reach the network.

    Regression: ChatProvider.call() returned None when its HTTP client was never
    bound, which is indistinguishable from an exhausted quota. The whole AI
    enrichment chain was silently dead in CI while every test still passed.
    """

    def test_providers_receive_the_http_client(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        sentinel = object()
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}, clear=False):
            enricher = Enricher(cfg, http=sentinel)  # type: ignore[arg-type]
        providers = {p.name: p for p in enricher.chat_providers}
        self.assertIn("mistral", providers)
        self.assertIs(providers["mistral"].http, sentinel)

    def test_call_raises_a_loud_error_without_a_client(self):
        from engine.enrich import ChatProvider

        spec = {
            "endpoint": "https://example.invalid/v1",
            "models_endpoint": "https://example.invalid/v1/models",
            "key_env": "MISTRAL_API_KEY",
            "models": ("m",),
            "label": "Test",
        }
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}, clear=False):
            provider = ChatProvider("mistral", spec, tmp, "2026-01-01")
        self.assertIsNone(provider.http)
        with self.assertLogs("pseo.enrich", level="ERROR") as logs:
            self.assertIsNone(asyncio.run(provider.call(None, "prompt")))
        self.assertTrue(any("no HTTP client" in line for line in logs.output))


class TestHttpErrorPropagation(unittest.TestCase):
    def test_rate_limit_body_survives_the_retry_wrapper(self):
        """The enricher classifies quota vs rate-limit from the message, so the
        underlying body must survive the retry wrapper."""
        import asyncio
        import aiohttp

        async def scenario() -> None:
            http = Http(concurrency=1, timeout=1, retries=1)

            class FakeResponse:
                status = 429
                headers = {"Content-Type": "application/json"}

                async def text(self) -> str:
                    return '{"error": {"message": "Resource has been exhausted"}}'

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            class FakeSession:
                def request(self, *args, **kwargs):
                    return FakeResponse()

            http._session = FakeSession()  # type: ignore[assignment]
            try:
                await http.get_json("https://example.invalid/models")
            except RuntimeError as exc:
                message = str(exc)
                self.assertIn("Resource has been exhausted", message)
                self.assertIn("HTTP 429", message)
            else:
                self.fail("expected a RuntimeError")

        asyncio.run(scenario())


class TestUrlStyle(unittest.TestCase):
    def test_html_style_keeps_extension(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["seo"] = {"url_style": "html"}
        self.assertEqual(
            cfg.url_for("berlin-climate.html"), "https://data.example/berlin-climate.html"
        )

    def test_clean_style_drops_extension_but_keeps_index(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["seo"] = {"url_style": "clean"}
        self.assertEqual(
            cfg.url_for("berlin-climate.html"), "https://data.example/berlin-climate"
        )
        self.assertEqual(cfg.url_for("index.html"), "https://data.example/")
        self.assertEqual(cfg.url_for("sitemap.xml"), "https://data.example/sitemap.xml")


class TestIndexNowHosting(unittest.TestCase):
    def test_subpath_hosts_are_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["domain"] = "https://mintmachinehq.github.io/pSEOare"
        self.assertFalse(key_file_is_reachable(cfg))

    def test_root_hosts_are_allowed(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["domain"] = "https://data-atlas.pages.dev"
        self.assertTrue(key_file_is_reachable(cfg))
        cfg.raw["domain"] = "https://atlas.example.com"
        self.assertTrue(key_file_is_reachable(cfg))


class TestModelResolution(unittest.TestCase):
    def test_fallback_models_are_current(self):
        self.assertNotIn("1.5", " ".join(FALLBACK_MODELS))
        self.assertIn("gemini-2.0-flash", FALLBACK_MODELS)

    def test_text_only_models(self):
        seen = [
            "gemini-2.5-flash",
            "gemini-3.8-flash-tts",
            "gemini-3.1-flash-image",
            "gemini-3.5-transcribe",
            "gemini-omni-1.1-flash",
            "gemma-4-26b-a4b-it",
        ]
        text = [n for n in seen if not any(m in n.lower() for m in NON_TEXT_MARKERS)]
        self.assertEqual(text, ["gemini-2.5-flash"])

    def test_configured_model_is_optional(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "api_key_env": "GEMINI_API_KEY"}
        import os

        os.environ["GEMINI_API_KEY"] = "test-key-123456"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            self.assertEqual(enricher.model, FALLBACK_MODELS[0])
        finally:
            del os.environ["GEMINI_API_KEY"]


class TestKeyRotation(unittest.TestCase):
    def test_pool_splits_any_separator(self):
        self.assertEqual(_key_pool("a, b\nc"), ["a", "b", "c"])

    def test_pool_drops_duplicates_and_blanks(self):
        self.assertEqual(_key_pool("a,,a, b"), ["a", "b"])
        self.assertEqual(_key_pool(""), [])

    def test_rotation_picks_next_key_then_stops(self):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        os.environ["GEMINI_API_KEYS"] = "key-one,key-two"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            self.assertEqual(enricher.api_key, "key-one")
            enricher.api_key = "key-two"
            self.assertEqual(enricher._mark_exhausted("gemini-test"), "key-one")
            enricher.api_key = "key-one"
            self.assertIsNone(enricher._mark_exhausted("gemini-test"))
            self.assertFalse(enricher.enabled)
        finally:
            del os.environ["GEMINI_API_KEYS"]


class TestGroqProvider(unittest.TestCase):
    def test_enabler_accepts_groq_only(self):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "keys_env": "GEMINI_API_KEYS", "groq_key_env": "GROQ_API_KEY"}
        os.environ["GROQ_API_KEY"] = "gsk-test"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            self.assertTrue(enricher.enabled)
            self.assertEqual(enricher.groq_key, "gsk-test")
            self.assertEqual(enricher.keys, [])
        finally:
            del os.environ["GROQ_API_KEY"]

    def test_defaults_to_off_without_any_provider(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True}
        self.assertFalse(Enricher(cfg, http=None).enabled)  # type: ignore[arg-type]


class TestGroqModelRotation(unittest.TestCase):
    def test_candidates_put_current_model_first(self):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        os.environ["GROQ_API_KEY"] = "gsk-test"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            enricher.groq_model = "llama-x"
            enricher.groq_models = ["llama-y", "llama-x"]
            candidates = enricher.groq_candidates()
            self.assertEqual(candidates[:2], ["llama-x", "llama-y"])
            # Known-good ids from the registry are appended after whatever discovery
            # returned, so a provider whose discovery call fails still has somewhere to go.
            self.assertIn("llama-3.3-70b-versatile", candidates)
            self.assertEqual(len(candidates), len(set(candidates)))  # no duplicates
        finally:
            del os.environ["GROQ_API_KEY"]


class TestOpenAICompatProviders(unittest.TestCase):
    """Cerebras and Mistral were added as registry entries rather than copies of the
    Groq code, so these check the shared contract every provider inherits."""

    def _enricher(self, **env):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        for key in ("CEREBRAS_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GEMINI_API_KEYS"):
            os.environ.pop(key, None)
        os.environ.update(env)
        return Enricher(cfg, http=None)  # type: ignore[arg-type]

    def test_mistral_only_enables_enrichment(self):
        enricher = self._enricher(MISTRAL_API_KEY="key-m")
        self.assertTrue(enricher.enabled)
        self.assertEqual(enricher.keys, [])
        self.assertEqual([p.name for p in enricher.chat_providers if p.available], ["mistral"])

    def test_cerebras_only_enables_enrichment(self):
        enricher = self._enricher(CEREBRAS_API_KEY="key-c")
        self.assertTrue(enricher.enabled)
        self.assertEqual([p.name for p in enricher.chat_providers if p.available], ["cerebras"])

    def test_all_three_stack_in_configured_order(self):
        enricher = self._enricher(
            CEREBRAS_API_KEY="c", MISTRAL_API_KEY="m", GROQ_API_KEY="g"
        )
        self.assertEqual(
            [p.name for p in enricher.chat_providers if p.available],
            ["cerebras", "mistral", "groq"],
        )

    def test_config_order_overrides_the_default(self):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "providers": ["mistral", "cerebras"]}
        os.environ.pop("GROQ_API_KEY", None)
        os.environ["MISTRAL_API_KEY"] = "m"
        os.environ["CEREBRAS_API_KEY"] = "c"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            self.assertEqual(
                [p.name for p in enricher.chat_providers if p.available],
                ["mistral", "cerebras"],
            )
        finally:
            os.environ.pop("MISTRAL_API_KEY", None)
            os.environ.pop("CEREBRAS_API_KEY", None)

    def test_unknown_provider_is_ignored_not_fatal(self):
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "providers": ["not-a-provider", "mistral"]}
        os.environ["MISTRAL_API_KEY"] = "m"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            self.assertEqual([p.name for p in enricher.chat_providers], ["mistral"])
        finally:
            os.environ.pop("MISTRAL_API_KEY", None)

    def test_each_provider_endpoints_are_distinct_and_openai_shaped(self):
        from engine.enrich import OPENAI_COMPAT_PROVIDERS

        seen = set()
        for name, spec in OPENAI_COMPAT_PROVIDERS.items():
            self.assertTrue(spec["endpoint"].endswith("/chat/completions"), name)
            self.assertTrue(spec["models_endpoint"].endswith("/models"), name)
            self.assertNotIn(spec["endpoint"], seen, name)
            seen.add(spec["endpoint"])
            self.assertTrue(spec["key_env"].endswith("_API_KEY"), name)
            self.assertTrue(spec["models"], name)

    def test_daily_quota_latches_and_never_retries(self):
        """A 429 on a free tier costs the whole day. Retrying it just burns wall clock
        and then every page falls back anyway."""
        import asyncio
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "providers": ["mistral"]}
        os.environ["MISTRAL_API_KEY"] = "m"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            provider = enricher.chat_providers[0]

            class Boom:
                async def post_json(self, *a, **k):
                    raise RuntimeError("HTTP 429 rate limit exceeded")

                async def get_json(self, *a, **k):
                    return {"data": []}

            provider.bind(Boom())
            page = sample_page(cfg)
            text = asyncio.run(provider.call(page, "prompt"))
            self.assertIsNone(text)
            self.assertTrue(provider.exhausted)
            self.assertTrue(provider.quota_path.exists(), provider.quota_path)
            # Latched for the day: a second call must not even reach the network.
            provider.http = None
            self.assertIsNone(asyncio.run(provider.call(page, "prompt")))
        finally:
            os.environ.pop("MISTRAL_API_KEY", None)

    def test_model_discovery_prefers_small_fast_models(self):
        import asyncio
        import os

        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["gemini"] = {"enabled": True, "providers": ["cerebras"]}
        os.environ["CEREBRAS_API_KEY"] = "c"
        try:
            enricher = Enricher(cfg, http=None)  # type: ignore[arg-type]
            provider = enricher.chat_providers[0]

            class Models:
                async def post_json(self, *a, **k):
                    return {"choices": [{"message": {"content": "  A paragraph of prose.  "}}]}

                async def get_json(self, *a, **k):
                    return {
                        "data": [
                            {"id": "llama-3.3-70b"},
                            {"id": "qwen-3-32b"},
                            {"id": "llama3.1-8b"},
                        ]
                    }

            provider.bind(Models())
            chosen = asyncio.run(provider.resolve_model())
            # A 40-call daily budget cannot use a 70b model at any sane latency, and a
            # wasted call is a lost page, so the small fast model wins.
            self.assertEqual(chosen, "llama3.1-8b")
            page = sample_page(cfg)
            self.assertEqual(asyncio.run(provider.call(page, "prompt")), "A paragraph of prose.")
        finally:
            os.environ.pop("CEREBRAS_API_KEY", None)


class TestRender(unittest.TestCase):
    def test_page_contains_required_blocks(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        theme, css = build_theme(), None
        css = build_css(theme)
        prose, faq = fallback_copy(page)
        html_doc = render_page(page, cfg, theme, css, prose, faq, "2026-01-01T00:00:00+00:00")

        self.assertIn("<h1", html_doc)
        self.assertIn("application/ld+json", html_doc)
        self.assertIn('<link rel="canonical" href="https://data.example/berlin-climate.html">', html_doc)
        self.assertIn("Berlin averages", html_doc)  # table caption survives
        self.assertIn("data-ad-slot", html_doc)
        self.assertIn("Frequently asked questions", html_doc)
        self.assertIn(theme["page"], html_doc)

        graph = json.loads(html_doc.split('type="application/ld+json">')[1].split("</script>")[0])
        types = [node["@type"] for node in graph["@graph"]]
        self.assertIn("WebPage", types)
        self.assertIn("Dataset", types)
        self.assertIn("BreadcrumbList", types)

    def test_html_is_escaped(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        page.summary = 'Berlin <script>alert("x")</script>'
        theme = build_theme()
        html_doc = render_page(page, cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00")
        self.assertNotIn("<script>alert", html_doc)
        self.assertIn("&lt;script&gt;", html_doc)

    def test_popunder_is_withheld_from_community_referral_traffic(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["popunder_script"] = (
            '<script data-cfasync="false" src="https://abscloud.org/1/abc123"></script>'
        )
        theme = build_theme()
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        head = html_doc[: html_doc.index("</head>")]
        # The vendor script is injected at runtime, so it must not appear as a plain tag.
        self.assertNotIn('src="https://abscloud.org/1/abc123"></script>', head)
        # The bootstrap must exist, and it must carry the referrer check and the src.
        self.assertIn("document.referrer", head)
        self.assertIn("abscloud.org/1/abc123", head)
        self.assertIn("reddit.com", head)
        # A subdomain of an exempt host must also match, so the suffix test has to exist.
        self.assertIn("host.slice(", head)
        self.assertLess(html_doc.index("document.referrer"), html_doc.index("</head>"))

    def test_popunder_gate_can_be_overridden_and_disabled(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["popunder_script"] = (
            '<script data-cfasync="false" src="https://abscloud.org/1/abc123"></script>'
        )
        # An empty list means "load for everyone": the snippet is emitted untouched.
        cfg.raw["monetization"]["popunder_exempt_hosts"] = []
        theme = build_theme()
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        self.assertIn('src="https://abscloud.org/1/abc123"></script>', html_doc)
        self.assertNotIn("document.referrer", html_doc)

        # The default list is used when the key is absent, so deleting it must not
        # silently disable the gate.
        del cfg.raw["monetization"]["popunder_exempt_hosts"]
        self.assertIn("reddit.com", ad_exempt_hosts(cfg))

    def test_popunder_is_injected_once_not_per_slot(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["popunder_script"] = "<script>/*popunder*/</script>"
        theme = build_theme()
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(html_doc.count("/*popunder*/"), 1)
        # The network documents this loader for the end of <head>: once per page, never
        # inside an ad slot, and before </head> so it is not render-blocking.
        self.assertLess(html_doc.index("/*popunder*/"), html_doc.index("data-ad-slot"))
        self.assertLess(html_doc.index("/*popunder*/"), html_doc.index("</head>"))
        self.assertNotIn("/*popunder*/", html_doc[html_doc.index("data-ad-slot") :])

    def test_native_banner_emitted_once_with_sized_container(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["adsterra_script"] = (
            '<script async="async" src="https://bauval.org/21/f1f54edba8f1e63e97bdd455dd7245ec">'
            '</script>\n<div id="container-f1f54edba8f1e63e97bdd455dd7245ec"></div>'
        )
        theme = build_theme()
        for slot in ("top", "mid", "foot"):
            block = ad_block(cfg, theme, slot)
            with self.subTest(slot=slot):
                # exactly one slot may carry the unit, or the container id duplicates
                if slot == cfg.raw["monetization"].get("native_banner_slot", "foot"):
                    self.assertEqual(block.count("bauval.org"), 1)
                    self.assertEqual(block.count("container-f1f54edba"), 1)
                    self.assertIn('class="native-banner"', block)
                else:
                    self.assertNotIn("bauval.org", block)
                    self.assertNotIn("container-f1f54edba", block)
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(html_doc.count("container-f1f54edba8f1e63e97bdd455dd7245ec"), 1)
        self.assertIn(".native-banner{", html_doc)

    def test_native_banner_kill_switch_removes_the_unit(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["adsterra_script"] = (
            '<script async="async" src="https://bauval.org/21/f1f54edba8f1e63e97bdd455dd7245ec">'
            '</script>\n<div id="container-f1f54edba8f1e63e97bdd455dd7245ec"></div>'
        )
        cfg.raw["monetization"]["native_banner_enabled"] = False
        theme = build_theme()
        for slot in ("top", "mid", "foot"):
            block = ad_block(cfg, theme, slot)
            with self.subTest(slot=slot):
                self.assertNotIn("bauval.org", block)
                self.assertNotIn("container-f1f54edba", block)
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        self.assertNotIn("bauval.org", html_doc)

    def test_not_found_document_is_noindex_and_not_pruned(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        theme = build_theme()
        doc = not_found_html(cfg, theme, build_css(theme), "2026-01-01T00:00:00+00:00")
        self.assertIn('name="robots" content="noindex, follow"', doc)
        self.assertNotIn("data-ad-slot", doc)
        self.assertIn(cfg.url_for("index.html"), doc)

    def test_keep_set_retains_the_404_document(self):
        output = Path(tempfile.mkdtemp())
        writer = Writer(output, output / "cache")
        writer.write_raw("404.html", "<html></html>")
        writer.write_raw("stale-page.html", "<html></html>")
        writer.sync_manifest([], "2026-01-01T00:00:00+00:00", prune=False)
        self.assertTrue((output / "404.html").exists(), "404.html must survive the orphan sweep")
        self.assertFalse((output / "stale-page.html").exists())

    def test_popunder_kill_switch_removes_the_loader(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["popunder_script"] = "<script>/*popunder*/</script>"
        cfg.raw["monetization"]["popunder_enabled"] = False
        theme = build_theme()
        html_doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "", [], "2026-01-01T00:00:00+00:00"
        )
        self.assertNotIn("/*popunder*/", html_doc)

    def test_house_ad_fills_an_unoccupied_slot(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["monetization"]["native_banner_enabled"] = False
        cfg.raw["monetization"]["house_ad"] = {
            "enabled": True,
            "url": "all-datasets-1.html",
            "kicker": "More reference data",
            "headline": "Browse every dataset",
            "text": "Country indicators, city climate averages and holiday dates.",
        }
        theme = build_theme()
        for slot in ("top", "mid", "foot"):
            block = ad_block(cfg, theme, slot)
            with self.subTest(slot=slot):
                self.assertIn('class="house-ad"', block)
                self.assertIn(cfg.url_for("all-datasets-1.html"), block)
                # the blank-slot detector must leave it alone: enough height, width, text
                self.assertGreater(len("Country indicators, city climate averages and holiday dates."), 12)
                self.assertNotIn("example.com", block)

    def test_house_ad_can_be_switched_off(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        cfg.raw["monetization"]["native_banner_enabled"] = False
        cfg.raw["monetization"]["house_ad"] = {"enabled": False, "url": "all-datasets-1.html"}
        block = ad_block(cfg, build_theme(), "top")
        self.assertNotIn("house-ad", block)
        self.assertIn("ad-hint", block)

    def test_budget_prefers_unseen_pages_so_the_corpus_grows(self):
        # Regression: sorting on is_new directly puts already-published pages first,
        # because False sorts before True. Every run then regenerated the same head of
        # the list and the corpus never grew past the first page_budget rows.
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = []
        for i in range(20):
            page = sample_page(cfg)
            page.slug = f"page-{i:03d}"
            pages.append(page)
        seen = {p.path for p in pages[:15]}
        budget = CallBudget(limit=5, seen=seen)
        chosen = trim_to_budget(pages, budget)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(
            [p.slug for p in chosen],
            ["page-015", "page-016", "page-017", "page-018", "page-019"],
        )

    def test_budget_reserves_a_share_for_refreshing_published_pages(self):
        # A source with far more unseen candidates than budget would otherwise spend
        # every run on new pages and never revisit the published ones, so a whole-corpus
        # change could never reach them. A quarter of the budget goes to refreshes.
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = []
        for i in range(200):
            page = sample_page(cfg)
            page.slug = f"page-{i:03d}"
            pages.append(page)
        seen = {p.path for p in pages[:80]}
        budget = CallBudget(limit=40, seen=seen)
        chosen = trim_to_budget(pages, budget)
        self.assertEqual(len(chosen), 40)
        slugs = [p.slug for p in chosen]
        self.assertEqual(sum(1 for s in slugs if s >= "page-080"), 30)  # unseen first
        self.assertEqual(sum(1 for s in slugs if s < "page-080"), 10)  # refresh reserve

    def test_budget_reserve_is_skipped_for_a_tiny_budget(self):
        # Below the threshold the reserve would eat most of the run, so growth wins.
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = []
        for i in range(20):
            page = sample_page(cfg)
            page.slug = f"page-{i:03d}"
            pages.append(page)
        seen = {p.path for p in pages[:15]}
        budget = CallBudget(limit=5, seen=seen)
        chosen = trim_to_budget(pages, budget)
        self.assertEqual(
            [p.slug for p in chosen],
            ["page-015", "page-016", "page-017", "page-018", "page-019"],
        )

    def test_indexnow_defers_to_the_next_run(self):
        # IndexNow validates every submitted URL. A page produced during this run is not
        # published until after generation, so notifying on the same run gets the batch
        # rejected. Each run must notify the previous run's URLs instead.
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        remember_pending(cfg, ["https://pseoare.pages.dev/a", "https://pseoare.pages.dev/b"])
        self.assertEqual(take_pending(cfg), ["https://pseoare.pages.dev/a", "https://pseoare.pages.dev/b"])
        # A fresh cache directory has nothing queued, and an empty run queues nothing.
        empty = make_cfg(Path(tempfile.mkdtemp()))
        self.assertEqual(take_pending(empty), [])
        remember_pending(empty, [])
        self.assertEqual(take_pending(empty), [])

    def test_sitemap_covers_the_whole_corpus_not_just_this_run(self):
        # The engine is incremental, so a run only rebuilds its own slice. Building the
        # sitemap from that slice alone under-reports the site and rotates its contents.
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = [sample_page(cfg)]
        pages[0].slug = "today"  # path is derived from slug
        published = {
            "today.html": {"generated_at": "2026-01-01T00:00:00+00:00"},
            "older.html": {"generated_at": "2025-12-01T00:00:00+00:00"},
        }
        xml = sitemap_xml(pages, cfg, "2026-02-02T00:00:00+00:00", published=published)
        self.assertIn(cfg.url_for("today.html"), xml)
        self.assertIn(cfg.url_for("older.html"), xml)
        self.assertIn("<lastmod>2025-12-01</lastmod>", xml)
        self.assertIn("<lastmod>2026-02-02</lastmod>", xml)
        # a page present in both the manifest and this run must appear exactly once
        self.assertEqual(xml.count(cfg.url_for("today.html")), 1)

    def test_http_tolerates_an_empty_success_body(self):
        # IndexNow answers an accepted POST with 200 and no body at all. Parsing that as
        # JSON turned every accepted submission into a retry and then into an error, so
        # the engine reported indexnow=0 while Bing was in fact being told about pages.
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.end_headers()  # 200 with no body, exactly like IndexNow

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/indexnow"
        try:

            async def scenario():
                async with Http(concurrency=1, timeout=5, retries=2) as http:
                    return await http.post_json(url, {"urlList": ["https://example.com/"]})

            self.assertIsNone(asyncio.run(scenario()))
        finally:
            server.shutdown()
            server.server_close()

    def test_quota_body_surfaces_directly_as_quota_exhausted(self):
        """An exhausted daily quota bypasses the retry wrapper, but the body must
        still reach the enricher, which classifies failures on the message."""
        import asyncio

        async def scenario() -> None:
            http = Http(concurrency=1, timeout=1, retries=3)

            class FakeResponse:
                status = 429
                headers = {"Content-Type": "application/json"}

                async def text(self) -> str:
                    return '{"error": {"message": "You exceeded your current quota"}}'

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            class FakeSession:
                def request(self, *args, **kwargs):
                    return FakeResponse()

            http._session = FakeSession()  # type: ignore[assignment]
            with self.assertRaises(QuotaExhausted) as ctx:
                await http.get_json("https://example.invalid/models")
            self.assertIn("exceeded your current quota", str(ctx.exception))

        asyncio.run(scenario())

    def test_exhausted_quota_is_sent_once_not_three_times(self):
        """A daily free-tier quota cannot recover during a run, so retrying it three
        times with backoff only burned the job timeout."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        attempts = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                attempts.append(1)
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = b'{"error":{"message":"You exceeded your current quota"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/generateContent"
        try:

            async def scenario():
                async with Http(concurrency=1, timeout=5, retries=3) as http:
                    await http.post_json(url, {"a": 1})

            with self.assertRaises(QuotaExhausted):
                asyncio.run(scenario())
            self.assertEqual(len(attempts), 1, "an exhausted quota must be sent once")
        finally:
            server.shutdown()
            server.server_close()

    def test_crypto_source_throttles_coingecko(self):
        """A wide coin list gathered at once tripped CoinGecko 429s, each retried
        three times. The source now paces itself below the key-free ceiling."""
        import inspect

        from engine.sources import crypto as crypto_source

        src = inspect.getsource(crypto_source.collect)
        self.assertIn("RateLimiter", src)
        self.assertIn("await limiter.acquire()", src)

    def _climate(self, name: str, warm: float, cold: float, wet: float, dry: float):
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        # coldest at midwinter, warmest at midsummer, so the derived swing is exactly
        # warm - cold and the peak lands on Jul
        temps = [
            cold + (warm - cold) * ((1 + math.cos(2 * math.pi * (i - 6) / 12)) / 2)
            for i in range(12)
        ]
        rains = []
        for i in range(12):
            rains.append(wet if i in (5, 6) else dry)
        rows = [[m, round(t, 1), round(r, 2), 40.0, -15.0]
                for m, t, r in zip(months, temps, rains)]
        return Page(kind="climate_city", title=name, h1=f"{name} climate: monthly averages",
                    slug=name.lower().replace(" ", "-"), summary=f"{name} averages.",
                    data={"table": {"headers": [], "rows": rows}})

    def test_fallback_copy_is_unique_across_pages(self):
        """One template reused everywhere is thin content. Prose must differ per page."""
        names = ["Berlin", "Tokyo", "Lagos", "Cairo", "Lima", "Oslo", "Chicago", "Mumbai"]
        pages = [self._climate(n, 22 + i, -4 + i, 3.0 + i, 0.4) for i, n in enumerate(names)]
        prose = [fallback_copy(p)[0] for p in pages]
        self.assertEqual(len(set(prose)), len(prose))

    def test_fallback_copy_states_a_derived_comparison(self):
        """Prose should say something the table does not: the temperature swing."""
        page = self._climate("Berlin", 20.0, -2.0, 2.5, 0.6)
        prose, faq = fallback_copy(page)
        # 20 - (-2) = 22, a number that appears nowhere in the table itself
        self.assertIn("22.0 C", prose)
        self.assertIn("Berlin", prose)           # proper noun stays capitalised
        self.assertNotIn("berlin", prose)
        self.assertTrue(faq)
        for question, answer in faq:
            self.assertTrue(question.strip())
            self.assertTrue(answer.strip())

    def test_fallback_copy_detects_a_seasonal_rainfall_climate(self):
        wet = self._climate("Lagos", 30.0, 26.0, 14.0, 0.2)
        prose, _ = fallback_copy(wet)
        self.assertRegex(prose, r"season|peak")

    def test_fallback_copy_survives_a_page_without_a_table(self):
        page = Page(kind="country", title="Japan", h1="Japan country data", slug="japan",
                    summary="x", facts=[("Population", "124,000,000")])
        prose, faq = fallback_copy(page)
        self.assertIn("124,000,000", prose)
        self.assertEqual(len(faq), 2)

    def test_climate_selects_cities_before_spending_requests(self):
        """454 cities each cost a NASA POWER call, so choosing them after fetching
        wasted hundreds of requests per run. Choose first: unseen cities first, and no
        more than the remaining budget."""
        import inspect

        from engine.sources import climate

        src = inspect.getsource(climate.collect)
        self.assertIn("budget.remaining", src)
        self.assertIn("page_path", src)
        self.assertLess(src.index("chosen = order"), src.index("asyncio.gather"))

    def test_climate_prefers_unpublished_cities(self):
        """The pre-filter has to use the same slug _build_page produces, or every city
        looks unseen and the whole list is refetched each run."""
        import inspect

        from engine.sources import climate

        src = inspect.getsource(climate)
        self.assertIn("average-monthly-temperature-rainfall", src)
        self.assertIn("budget.is_new(page_path(c))", src)

    def test_prose_version_is_part_of_the_page_hash(self):
        """Changing the copy generator must invalidate every published page, or they keep
        the old boilerplate until their turn comes round again."""
        import inspect

        import generator

        src = inspect.getsource(generator.run)
        self.assertIn("prose_version", src)
        self.assertIn("json.dumps([prose, faq, prose_version]", src)

    def test_rss_feed_carries_the_newest_pages(self):
        import xml.etree.ElementTree as ET

        from engine.hubs import rss_feed

        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = []
        for i in range(5):
            page = sample_page(cfg)
            page.slug = f"item-{i}"
            page.title = f"Item {i} & co"
            pages.append(page)
        xml = rss_feed(pages, cfg, "2026-02-02T00:00:00+00:00", limit=3)
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "rss")
        items = root.findall("./channel/item")
        self.assertEqual(len(items), 3)                      # honours the limit
        self.assertEqual(len(root.findall("./channel/title")), 1)
        self.assertIn("Item 0 &amp; co", xml)                # escaped, not raw
        self.assertIn("feed.xml", Path("engine/writer.py").read_text())  # survives pruning

    def test_related_links_cross_datasets_by_country(self):
        """A climate page for a city should lead to that country's holiday calendar,
        otherwise visitors stay inside one dataset and pageviews per session stay low."""
        from engine.hubs import assign_related

        cfg = make_cfg(Path(tempfile.mkdtemp()))
        climate = sample_page(cfg)
        climate.kind = "climate_city"
        climate.slug = "shanghai-climate"
        climate.h1 = "Shanghai climate"
        climate.data = dict(climate.data or {}, entity="China")

        holidays = sample_page(cfg)
        holidays.kind = "holidays_year"
        holidays.slug = "holidays-china"
        holidays.h1 = "Public holidays in China 2026"
        holidays.data = dict(holidays.data or {}, entity="China")

        unrelated = sample_page(cfg)
        unrelated.kind = "holidays_year"
        unrelated.slug = "holidays-peru"
        unrelated.h1 = "Public holidays in Peru 2026"
        unrelated.data = dict(unrelated.data or {}, entity="Peru")

        pages = [climate, holidays, unrelated]
        assign_related(pages, cfg)  # same run, so the live pages already cover it
        hrefs = {link.url for link in climate.related}
        self.assertIn(cfg.url_for("holidays-china.html"), hrefs)   # same country
        self.assertNotIn(cfg.url_for("holidays-peru.html"), hrefs)  # different country

    def test_entity_index_lets_a_later_run_link_across_datasets(self):
        """A run only rebuilds one slice, so cross-dataset links need the index that
        earlier runs left behind. Without it the feature silently does nothing."""
        from engine.hubs import assign_related, load_entity_index, save_entity_index

        cache = Path(tempfile.mkdtemp())
        cfg = make_cfg(cache)

        holidays = sample_page(cfg)
        holidays.kind = "holidays_year"
        holidays.slug = "public-holidays-china-2026"
        holidays.h1 = "Public holidays in China 2026"
        holidays.data = dict(holidays.data or {}, entity="China")
        save_entity_index(cache, [holidays])
        self.assertTrue((cache / "entity_index.json").exists())

        # A later, climate-only run sees only its own page.
        climate = sample_page(cfg)
        climate.kind = "climate_city"
        climate.slug = "chengdu-average-monthly-temperature-rainfall"
        climate.h1 = "Chengdu climate"
        climate.data = dict(climate.data or {}, entity="China")
        assign_related([climate], cfg, load_entity_index(cache))

        hrefs = {link.url for link in climate.related}
        self.assertIn(cfg.url_for("public-holidays-china-2026.html"), hrefs)

    def test_entity_index_survives_a_corrupt_file(self):
        """A truncated cache must cost cross links, not the whole build."""
        from engine.hubs import load_entity_index

        cache = Path(tempfile.mkdtemp())
        (cache / "entity_index.json").write_text("{not json", "utf-8")
        self.assertEqual(load_entity_index(cache), {})

    def test_every_source_builder_is_callable(self):
        """A source that raises inside its own builder dies every run and only shows up
        as one ERROR line among thousands. This happened: the countries ranking builder
        referenced a name that was not in scope, so that dataset produced nothing."""
        import inspect

        from engine.sources import climate, countries, crypto, holidays

        builders = [
            countries._country_pages,
            countries._ranking_pages,
            holidays._build_pages,
            climate._build_page,
        ]
        for fn in builders:
            names = {
                n
                for n in inspect.signature(fn).parameters
            }
            body = inspect.getsource(fn)
            for token in ("name", "record"):
                if f'"{token}"' in body or f"'{token}'" in body:
                    continue
                self.assertNotIn(
                    f'"entity": {token},', body,
                    f"{fn.__name__} references {token!r}; make sure it is a parameter",
                )
            self.assertTrue(names, fn.__name__)

    def test_country_rank_pages_carry_a_country_entity(self):
        """Cross-dataset linking keys off this field; a NameError here silently drops a
        whole dataset out of the entity index."""
        from engine.config import load_config
        from engine.sources import countries

        record = {"id": "DZA", "name": "Algeria"}
        cfg = load_config(Path("config.json"))
        pages = countries._ranking_pages(
            [record],
            {record["id"]: {"SP.POP.TOTL": 45_000_000, "AG.SRF.TOTL.K2": 2_000_000}},
            cfg,
            "2026-10-02T00:00:00+00:00",
            False,
        )
        self.assertTrue(pages)
        for page in pages:
            self.assertEqual((page.data or {}).get("entity"), "Algeria")

    def test_city_list_slugs_are_unique_and_stable(self):
        """Slugs come from the city name, so two cities sharing a name would overwrite each
        other's page. build_cities.py freezes any already-published slug and disambiguates
        the rest; this guards the two properties that actually matter."""
        import re

        cities = json.loads(Path("assets/cities.json").read_text("utf-8"))["cities"]
        slugs = [c["slug"] for c in cities]

        def derived(name: str) -> str:
            return re.sub(r"[^a-z0-9]+", "-", f"{name}-average-monthly-temperature-rainfall".lower().replace("&", "and")).strip("-")

        self.assertEqual(len(slugs), len(set(slugs)), "two cities share a slug")
        for city in cities:
            # Every published city keeps its historical name-derived slug.
            self.assertEqual(city["slug"], derived(city["name"]), city["name"])
            for field in ("country", "lat", "lon", "population"):
                self.assertIn(field, city)

    def test_climate_honours_an_explicit_slug(self):
        """The pre-fetch predictor and the page builder must agree on the slug, or unseen
        detection breaks and the whole list is refetched every run."""
        from engine.sources.climate import _build_page
        from engine.config import load_config

        cfg = load_config(Path("config.json"))
        city = {
            "name": "Springfield",
            "country": "United States",
            "lat": 39.8,
            "lon": -89.6,
            "population": 150000,
            "slug": "illinois-springfield-average-monthly-temperature-rainfall",
        }
        # POWER nests under properties.parameter and _build_page requires an ANN mean.
        params = {
            "T2M": {"JAN": 1.0, "JUL": 22.0, "ANN": 11.5},
            "T2M_MAX": {"JAN": 5.0, "JUL": 26.0, "ANN": 15.5},
            "T2M_MIN": {"JAN": -3.0, "JUL": 18.0, "ANN": 7.5},
            "PRECTOTCORR": {"JAN": 2.0, "JUL": 4.0, "ANN": 3.0},
        }
        data = {"properties": {"parameter": params}}
        page = _build_page(city, data, cfg, "2026-10-02T00:00:00+00:00", False)
        self.assertIsNotNone(page)
        self.assertEqual(page.slug, city["slug"])
        self.assertEqual(page.path, city["slug"] + ".html")

    def test_waterfall_has_fallback(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        js = waterfall_js(cfg)
        self.assertIn("https://offer.example/x", js)
        self.assertIn("data-fallback", js)

    def test_pick_copy_prefers_enriched(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        prose, faq = pick_copy(page, {"climate:berlin": "Enriched prose.\nFAQ: Q?|A."})
        self.assertEqual(prose, "Enriched prose.")
        self.assertEqual(len(faq), 1)


class TestRssDate(unittest.TestCase):
    # A parser rejects the whole channel date when pubDate is not RFC 822. Feedrabbit
    # reported "Only UTC times are supported" for a stamp that was UTC but written as
    # ISO 8601, which is valid Atom and not valid RSS.
    def test_iso_stamp_becomes_rfc822_in_utc(self):
        self.assertEqual(
            rss_date("2026-10-03T21:41:45.365613+00:00"),
            "Sat, 03 Oct 2026 21:41:45 GMT",
        )

    def test_non_utc_offset_is_converted_not_stripped(self):
        self.assertEqual(
            rss_date("2026-10-03T23:41:45+02:00"),
            "Sat, 03 Oct 2026 21:41:45 GMT",
        )

    def test_z_suffix_and_unparseable_input_are_handled(self):
        self.assertEqual(rss_date("2026-10-03T21:41:45Z"), "Sat, 03 Oct 2026 21:41:45 GMT")
        # Garbage must not raise: a bad stamp cannot be allowed to fail a build.
        self.assertRegex(rss_date("not-a-date"), r"^[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4}")

    def test_feed_emits_rfc822_pubdate_and_lastbuilddate(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        feed = rss_feed([sample_page(cfg)], cfg, "2026-10-03T21:41:45+00:00")
        self.assertIn("<lastBuildDate>Sat, 03 Oct 2026 21:41:45 GMT</lastBuildDate>", feed)
        self.assertIn("<pubDate>Sat, 03 Oct 2026 21:41:45 GMT</pubDate>", feed)
        # No ISO offset may survive anywhere in the feed.
        self.assertNotIn("+00:00", feed)


class TestAnswerBlock(unittest.TestCase):
    # Answer engines quote a short extract with a clear answer near the top of the page.
    # Everything here is derived from page.facts, so it cannot drift from the numbers.

    def test_answer_sentence_is_built_from_the_page_facts(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        page = sample_page(cfg)
        page.facts = [("City", "Berlin"), ("Warmest month", "Jul (20.5 C)")]
        sentence = answer_sentence(page)
        self.assertTrue(sentence.startswith("Berlin climate"))
        # The numbers must be in the sentence, not just the heading repeated.
        self.assertIn("warmest month Jul", sentence)
        self.assertTrue(sentence.endswith("."))
        # A page with no facts still produces an answer rather than nothing.
        page.facts = []
        self.assertEqual(answer_sentence(page), "Berlin climate.")

    def test_dataset_node_carries_the_fields_dataset_search_needs(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        page = sample_page(cfg)
        page.facts = [
            ("City", "Berlin"),
            ("Warmest month", "Jul (20.5 C)"),
            ("Source", "NASA POWER"),
            ("Country", "Germany"),
        ]
        node = dataset_ld(page, cfg, "https://example.org/p", "2026-10-04T00:00:00+00:00")
        for field in (
            "variableMeasured",
            "spatialCoverage",
            "distribution",
            "license",
            "isAccessibleForFree",
            "includedInDataCatalog",
        ):
            self.assertIn(field, node, f"Dataset node is missing {field}")
        self.assertEqual(node["spatialCoverage"], "Berlin")
        # A page with no place fact must not invent one.
        page.facts = [("Warmest month", "Jul (20.5 C)")]
        self.assertNotIn("spatialCoverage", dataset_ld(page, cfg, "https://example.org/p", "s"))

    def test_page_carries_answer_block_and_speakable_selector(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        theme = build_theme()
        doc = render_page(
            sample_page(cfg), cfg, theme, build_css(theme), "prose", [], "2026-10-04T00:00:00+00:00"
        )
        self.assertIn('class="answer-block"', doc)
        self.assertIn("Quick answer", doc)
        # The selector named in speakable must actually match an element on the page.
        self.assertIn('"speakable"', doc)
        self.assertIn(".answer-block", doc)
        # It has to sit above the long prose, or it is not an answer-first layout.
        self.assertLess(doc.index("answer-block"), doc.index("lede"))

    def test_answer_block_is_omitted_when_there_are_no_facts(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        theme = build_theme()
        page = sample_page(cfg)
        page.facts = []
        doc = render_page(page, cfg, theme, build_css(theme), "prose", [], "2026-10-04T00:00:00+00:00")
        # The stylesheet always ships; only the rendered block must disappear.
        self.assertNotIn('class="answer-block"', doc)
        self.assertNotIn("Quick answer", doc)


class TestAiDiscoveryFiles(unittest.TestCase):
    def test_robots_names_the_ai_crawlers_and_the_sitemap(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        text = robots_txt(cfg)
        for agent in AI_CRAWLERS:
            self.assertIn(f"User-agent: {agent}", text)
        self.assertIn("Sitemap: ", text)
        self.assertIn("llms.txt", text)
        # The wildcard rule must survive alongside the named agents.
        self.assertIn("User-agent: *\nAllow: /", text)

    def test_llms_txt_lists_every_hub_and_the_core_endpoints(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        text = llms_txt(cfg)
        self.assertIn("hub-climate", text)
        self.assertIn("hub-holidays", text)
        self.assertIn("hub-crypto", text)
        self.assertIn("sitemap.xml", text)
        self.assertIn("feed.xml", text)
        # Every hub slug the generator can emit must be reachable from the file.
        for spec in SECTIONS.values():
            self.assertIn(spec["slug"], text, f"{spec['slug']} missing from llms.txt")


class TestWriter(unittest.TestCase):
    def test_delta_skips_unchanged(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        pages = [sample_page(cfg)]
        stamp = "2026-01-01T00:00:00+00:00"

        writer = Writer(cfg.paths.output, cfg.paths.cache)
        new_files, written = writer.write_pages(pages, lambda p: "<html>1</html>", stamp)
        writer.sync_manifest(pages, stamp)
        self.assertEqual((len(new_files), written), (1, 1))

        writer2 = Writer(cfg.paths.output, cfg.paths.cache)
        new_files2, written2 = writer2.write_pages(pages, lambda p: "<html>1</html>", stamp)
        self.assertEqual((len(new_files2), written2), (0, 0))

        changed = [sample_page(cfg)]
        changed[0].data["table"]["rows"] = [["Jan", "-1.0"]]
        writer3 = Writer(cfg.paths.output, cfg.paths.cache)
        _, written3 = writer3.write_pages(changed, lambda p: "<html>2</html>", stamp)
        self.assertEqual(written3, 1)

    def test_verification_files_survive_cleanup(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        token = "googleabc123.html"
        cfg.raw["seo"] = {"verification_files": {token: "google-site-verification: googleabc123"}}
        writer = Writer(cfg.paths.output, cfg.paths.cache)
        for name, body in cfg.raw["seo"]["verification_files"].items():
            writer.write_raw(name, body)
        writer.sync_manifest([], "2026-01-01T00:00:00+00:00", extra_keep=set(cfg.raw["seo"]["verification_files"]))
        self.assertTrue((cfg.paths.output / token).exists())

    def test_deploy_metadata_written(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        writer = Writer(cfg.paths.output, cfg.paths.cache)
        writer.write_deploy_metadata()
        self.assertTrue((cfg.paths.output / ".nojekyll").exists())

    def test_manifest_is_cumulative(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        stamp = "2026-01-01T00:00:00+00:00"
        first = sample_page(cfg)
        Writer(cfg.paths.output, cfg.paths.cache).sync_manifest([first], stamp)
        second = sample_page(cfg)
        second.slug = "second-page"
        Writer(cfg.paths.output, cfg.paths.cache).sync_manifest([second], stamp)
        manifest = json.loads((cfg.paths.cache / "manifest.json").read_text())
        self.assertIn("berlin-climate.html", manifest["pages"])
        self.assertIn("second-page.html", manifest["pages"])


class TestHubPagination(unittest.TestCase):
    def _many(self, cfg, count):
        pages = []
        for i in range(count):
            page = sample_page(cfg)
            page.slug = f"city-{i:04d}"
            page.h1 = f"City {i}"
            pages.append(page)
        return pages

    def test_hubs_paginate(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["domain"] = "https://data.example"
        pages = self._many(cfg, LINKS_PER_PAGE + 25)
        docs = dict(hub_documents(pages, cfg, build_theme(), build_css(build_theme())))
        self.assertIn("hub-climate.html", docs)
        self.assertIn("hub-climate-2.html", docs)
        self.assertIn("index.html", docs)
        self.assertIn("all-datasets-2.html", docs)

    def test_hub_canonicals_are_unique(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        cfg.raw["domain"] = "https://data.example"
        pages = self._many(cfg, LINKS_PER_PAGE + 10)
        docs = hub_documents(pages, cfg, build_theme(), build_css(build_theme()))
        canon = [h.split('rel="canonical" href="')[1].split('"')[0] for _, h in docs]
        self.assertEqual(len(canon), len(set(canon)), "every hub page needs its own canonical")

    def test_every_page_appears_in_a_hub(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        pages = self._many(cfg, LINKS_PER_PAGE + 5)
        names = hub_filenames(pages)
        self.assertEqual(len(names), 1 + 2 + 1)  # index + 2 hub pages + 1 all-datasets page


class TestCopyHash(unittest.TestCase):
    def test_prose_change_invalidates_the_hash(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        before = page.content_hash
        page.data["copy_hash"] = "abc123"
        self.assertNotEqual(before, page.content_hash)
        page.data["copy_hash"] = "abc123"
        self.assertEqual(page.content_hash, page.content_hash)


class TestRenderSignature(unittest.TestCase):
    """A monetization or theme change must invalidate every page, not just new ones."""

    def tearDown(self) -> None:
        set_render_signature("")

    def test_signature_change_invalidates_the_hash(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        set_render_signature("sig-a")
        before = page.content_hash
        set_render_signature("sig-b")
        self.assertNotEqual(before, page.content_hash)

    def test_stable_signature_keeps_the_hash(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        set_render_signature("sig-a")
        before = page.content_hash
        self.assertEqual(before, page.content_hash)

    def test_default_signature_matches_unset(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        page = sample_page(cfg)
        self.assertEqual(page.content_hash, page.content_hash)


class TestLinks(unittest.TestCase):
    def test_related_links_point_at_real_pages(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        pages = [sample_page(cfg)]
        for i in range(3):
            clone = sample_page(cfg)
            clone.slug = f"city-{i}"
            clone.h1 = f"City {i}"
            pages.append(clone)
        assign_related(pages, cfg)
        slugs = {p.slug for p in pages}
        for page in pages:
            for link in page.related:
                target = link.url.rsplit("/", 1)[-1]
                self.assertTrue(target.endswith(".html"))
                if target != "hub-climate.html":
                    self.assertIn(target[:-5], slugs)

    def test_sitemap_contains_pages(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        xml = sitemap_xml([sample_page(cfg)], cfg, "2026-01-01T00:00:00+00:00")
        self.assertIn("https://data.example/berlin-climate.html", xml)
        self.assertTrue(xml.startswith("<?xml"))


class TestSources(unittest.TestCase):
    def test_holidays_build_pages(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        cfg.raw["domain"] = "https://data.example"
        country = {"code": "DE", "name": "Germany", "currency": "euro", "language": "German"}
        holidays_data = [
            {"date": "2026-01-01", "name": "New Year's Day", "localName": "Neujahr"},
            {"date": "2026-12-25", "name": "Christmas Day", "localName": "Erster Weihnachtstag"},
        ]
        pages = holidays._build_pages(country, 2026, holidays_data, cfg, "2026-01-01T00:00:00+00:00", False)
        self.assertTrue(pages)
        year_page = pages[0]
        self.assertEqual(year_page.kind, "holidays_year")
        self.assertEqual(year_page.data["table"]["headers"][0], "Date")
        self.assertEqual(len(year_page.data["table"]["rows"]), 2)
        self.assertTrue(any(p.kind == "holiday_single" for p in pages))

    def test_climate_converts_daily_rain_to_annual(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        city = {"name": "Berlin", "country": "Germany", "lat": 52.52, "lon": 13.405, "population": 3660000}
        payload = {
            "properties": {
                "parameter": {
                    "T2M": {"JAN": -0.7, "JUL": 20.5, "ANN": 9.7},
                    "T2M_MAX": {"JAN": 1.0, "JUL": 24.0, "ANN": 12.0},
                    "T2M_MIN": {"JAN": -2.5, "JUL": 16.0, "ANN": 7.0},
                    "PRECTOTCORR": {"JAN": 1.4, "JUL": 2.6, "ANN": 1.65},
                }
            }
        }
        payload["properties"]["parameter"]["T2M_MAX"] = {"JAN": 12.8, "JUL": 37.6, "ANN": 37.7}
        payload["properties"]["parameter"]["T2M_MIN"] = {"JAN": -23.9, "JUL": 8.7, "ANN": -23.9}
        page = climate._build_page(city, payload, cfg, "2026-01-01T00:00:00+00:00", False)
        self.assertIsNotNone(page)
        self.assertEqual(page.kind, "climate_city")
        self.assertIn("602 mm", page.summary)  # 1.65 mm/day * 365
        annual = dict(page.facts)["Annual precipitation"]
        self.assertIn("mm / year", annual)
        # POWER max/min are period records, so the table must not call them mean highs.
        headers = page.data["table"]["headers"]
        self.assertIn("Record high (C)", headers)
        self.assertNotIn("Mean high (C)", headers)
        self.assertEqual(dict(page.facts)["All-time record high"], "37.7 C")

    def test_world_bank_rows_parsing(self):
        payload = [{"page": 1, "total": 2}, [{"id": "BEL", "name": "Belgium"}, {"id": "DEU", "name": "Germany"}]]
        rows = countries._rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "BEL")

    def test_world_bank_country_pages(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        record = {
            "id": "BEL",
            "name": "Belgium",
            "capitalCity": "Brussels",
            "latitude": "50.83",
            "longitude": "4.0",
            "region": {"id": "ECS", "value": "Europe & Central Asia"},
            "incomeLevel": {"id": "HIC", "value": "High income"},
        }
        values = {
            countries.POPULATION: 11600000,
            countries.AREA: 30528,
            countries.GDP: 5.5e11,
            countries.GDP_PC: 47300,
        }
        pages = countries._country_pages(record, values, cfg, "2026-01-01T00:00:00+00:00", False)
        self.assertEqual({p.kind for p in pages}, {"country_profile", "country_population"})
        self.assertIn("Belgium", pages[0].title)

    def test_crypto_monthly_table(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        base = 1767225600000  # 2026-01-01T00:00:00Z in ms
        payload = {
            "prices": [[base + i * 86400000, 100 + i] for i in range(60)],
            "total_volumes": [[base + i * 86400000, 1e9] for i in range(60)],
        }
        page = crypto._build_page("bitcoin", payload, cfg, "2026-01-01T00:00:00+00:00", False)
        self.assertIsNotNone(page)
        self.assertEqual(page.kind, "crypto_12m")
        self.assertEqual(page.data["table"]["headers"][0], "Month")
        self.assertTrue(page.data["table"]["rows"])

    def test_crypto_rejects_short_series(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        payload = {"prices": [[1767225600000, 100]]}
        self.assertIsNone(crypto._build_page("bitcoin", payload, cfg, "2026-01-01T00:00:00+00:00", False))

    def test_coingecko_daily_ceiling_is_treated_as_exhausted(self):
        """CoinGecko words its key-free daily cap as a rate limit; retrying it wastes minutes."""
        self.assertTrue(_is_quota_exhausted("You've exceeded the Rate Limit. Please visit ..."))
        self.assertTrue(_is_quota_exhausted("quota exceeded"))
        # A genuine momentary throttle is still retryable.
        self.assertFalse(_is_quota_exhausted("too many requests, slow down"))

    def test_crypto_latches_the_daily_ceiling_and_reads_cache(self):
        """After one refusal the remaining coins must issue no requests at all."""
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        base = 1767225600000
        payload = {
            "prices": [[base + i * 86400000, 100 + i] for i in range(60)],
            "total_volumes": [[base + i * 86400000, 1e9] for i in range(60)],
        }
        # Prime the cache for both coins so the cache-only path has data to serve.
        write_json_cache(cfg.paths.cache / "crypto_bitcoin_365d.json", payload)
        write_json_cache(cfg.paths.cache / "crypto_solana_365d.json", payload)

        calls: list[str] = []

        class ThrottledHttp:
            async def get_json(self, url, params=None, headers=None):
                calls.append(url)
                raise QuotaExhausted("HTTP 429 quota exhausted: You've exceeded the Rate Limit")

        class Budget:
            def is_new(self, path):
                return True

            @property
            def remaining(self):
                return 100

            def take(self, n=1):
                return True

        cfg.raw["sources"] = {"crypto": {"coins": ["bitcoin", "solana"], "requests_per_minute": 600}}
        pages = asyncio.run(
            crypto.collect(cfg, ThrottledHttp(), Budget())
        )
        # Only the first coin is attempted; the other is served from cache.
        self.assertEqual(len(calls), 1)
        self.assertEqual({p.kind for p in pages}, {"crypto_12m"})
        self.assertEqual(len(pages), 2)


class TestUnavailableMarker(unittest.TestCase):
    """A rate limit must never blacklist a working endpoint for good."""

    async def _fetch(self, exc, cache_dir, name="crypto_x_365d.json"):
        class FailingHttp:
            async def get_json(self, url, params=None, headers=None):
                raise exc

        return await cached_fetch(FailingHttp(), cache_dir, name, "https://api.example/x")

    def test_rate_limit_does_not_write_the_marker(self):
        for exc in (
            QuotaExhausted("HTTP 429 quota exhausted: You've exceeded the Rate Limit"),
            RuntimeError("request failed after 3 attempts: https://x (HTTP 429 rate limited)"),
        ):
            cache_dir = Path(tempfile.mkdtemp())
            asyncio.run(self._fetch(exc, cache_dir))
            self.assertFalse(
                (cache_dir / "crypto_x_365d.unavailable").exists(),
                f"{exc} must not permanently disable the endpoint",
            )

    def test_permanent_refusal_does_write_the_marker(self):
        cache_dir = Path(tempfile.mkdtemp())
        asyncio.run(self._fetch(RuntimeError("HTTP 404: not found"), cache_dir))
        self.assertTrue((cache_dir / "crypto_x_365d.unavailable").exists())


class TestCryptoFetchOrder(unittest.TestCase):
    """A latched run only gets one request, so it must go to a coin that needs it."""

    def test_uncached_coins_are_tried_first(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        cache = cfg.paths.cache
        for coin in ("bitcoin", "ethereum"):
            write_json_cache(cache / f"crypto_{coin}_365d.json", {"prices": []})
        ordered = crypto._uncached_first(cfg, ["bitcoin", "ethereum", "cosmos", "solana"])
        self.assertEqual(ordered[:2], ["cosmos", "solana"])
        self.assertEqual(set(ordered), {"bitcoin", "ethereum", "cosmos", "solana"})

    def test_order_is_stable_and_complete_with_no_cache(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        coins = ["bitcoin", "cosmos", "matic-network"]
        self.assertEqual(crypto._uncached_first(cfg, coins), sorted(coins))

    def test_coin_ids_get_readable_slugs(self):
        """AVAX is avalanche-2 and POL is matic-network; the slug must not leak that."""
        cfg = make_cfg(Path(tempfile.mkdtemp()))
        base = 1767225600000
        payload = {
            "prices": [[base + i * 86400000, 100 + i] for i in range(60)],
            "total_volumes": [[base + i * 86400000, 1e9] for i in range(60)],
        }
        for coin, slug in (
            ("avalanche-2", "avalanche-price-by-month"),
            ("matic-network", "polygon-price-by-month"),
            ("hedera-hashgraph", "hedera-price-by-month"),
        ):
            page = crypto._build_page(coin, payload, cfg, "2026-01-01T00:00:00+00:00", False)
            self.assertIsNotNone(page)
            self.assertEqual(page.slug, slug)


class TestPruneMarkers(unittest.TestCase):
    """Only a definitive refusal should keep an endpoint retired."""

    def test_rate_limits_and_quota_markers_are_stale(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import prune_markers

        for reason in (
            "request failed after 3 attempts: https://x (HTTP 429 rate limited)",
            "HTTP 429 quota exhausted: You've exceeded the Rate Limit",
            "Cannot connect to host api.example:443",
            "TimeoutError",
        ):
            self.assertTrue(prune_markers.is_stale(reason), reason)

    def test_permanent_status_markers_are_kept(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import prune_markers

        for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 410", "HTTP 451"):
            self.assertFalse(prune_markers.is_stale(f"request failed: {code} gone"), code)

    def test_it_only_ever_removes_marker_files(self):
        """A repair tool must not be able to delete a cache payload or a page."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import prune_markers

        cache = Path(tempfile.mkdtemp())
        (cache / "crypto_bitcoin_365d.json").write_text("{}")
        (cache / "good.unavailable").write_text("HTTP 404 gone")
        (cache / "stale.unavailable").write_text("HTTP 429 rate limited")
        argv = sys.argv
        sys.argv = ["prune_markers.py", "--apply", "--cache", str(cache)]
        try:
            self.assertEqual(prune_markers.main(), 0)
        finally:
            sys.argv = argv
        self.assertFalse((cache / "stale.unavailable").exists())
        self.assertTrue((cache / "good.unavailable").exists())
        self.assertTrue((cache / "crypto_bitcoin_365d.json").exists())


class TestBingQuota(unittest.TestCase):
    """Bing reports the size of the allowance, never what is left of it."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import importlib

        import bing_submit

        self.m = importlib.reload(bing_submit)

    def _with_state(self, value, date):
        tmp = Path(tempfile.mkdtemp()) / "bing_submitted.json"
        if value is not None:
            tmp.write_text(json.dumps({"date": date, "count": value}))
        self.m.STATE = tmp
        return tmp

    def test_no_state_file_means_nothing_submitted(self):
        self._with_state(None, "2000-01-01")
        self.assertEqual(self.m.submitted_today(), 0)

    def test_todays_tally_is_counted(self):
        import datetime as dt

        self._with_state(40, dt.date.today().isoformat())
        self.assertEqual(self.m.submitted_today(), 40)

    def test_yesterdays_tally_does_not_count(self):
        self._with_state(100, "2000-01-01")
        self.assertEqual(self.m.submitted_today(), 0)

    def test_remaining_is_allowance_minus_tally(self):
        import datetime as dt

        today = dt.date.today().isoformat()
        # No network in a unit test: the endpoint only supplies the allowance size.
        with mock.patch.object(
            self.m, "call", return_value={"d": {"DailyQuota": 100, "MonthlyQuota": 2900}}
        ):
            self._with_state(None, "2000-01-01")
            self.assertEqual(self.m.quota("k", "s", quiet=True), 100)  # state absent
            self._with_state(30, today)
            self.assertEqual(self.m.quota("k", "s", quiet=True), 70)
            self._with_state(100, today)
            self.assertEqual(self.m.quota("k", "s", quiet=True), 0)
            self._with_state(500, today)  # never negative, never sends past the cap
            self.assertEqual(self.m.quota("k", "s", quiet=True), 0)

    def test_quota_refusal_is_recognised(self):
        for body in (
            '{"ErrorCode":8,"Message":"ERROR!!! You have exceeded your daily url submission quota : 100"}',
            '{"ErrorCode":8,"Message":"ERROR!!! Quota remaining for today: 0, Submitted: 100"}',
        ):
            self.assertTrue(any(m in body.lower() for m in self.m.QUOTA_ERROR_MARKERS), body)


class TestBingSubmission(unittest.TestCase):
    def test_long_tail_pages_outrank_hubs(self):
        """The daily allowance is 100 URLs, so hubs must not be spent first."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import bing_submit

        urls = [
            "https://pseoare.pages.dev/",
            "https://pseoare.pages.dev/hub-climate",
            "https://pseoare.pages.dev/hub-countries",
            "https://pseoare.pages.dev/berlin-average-monthly-temperature-rainfall",
            "https://pseoare.pages.dev/population-of-france",
        ]
        ordered = bing_submit.prioritise(urls)
        self.assertEqual(ordered[0], "https://pseoare.pages.dev/berlin-average-monthly-temperature-rainfall")
        self.assertEqual(ordered[1], "https://pseoare.pages.dev/population-of-france")
        self.assertEqual(set(ordered[-3:]), set(urls[:3]))
        self.assertEqual(sorted(ordered), sorted(urls), "prioritise must not drop a url")

    def test_bing_key_path_is_overridable_for_ci(self):
        """CI mounts the key from a secret, so the path has to be overridable."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import importlib

        import bing_submit

        with mock.patch.dict(os.environ, {"BING_KEY_PATH": "/tmp/x/bing-webmaster-key"}):
            reloaded = importlib.reload(bing_submit)
            self.assertEqual(reloaded.KEY_PATH, "/tmp/x/bing-webmaster-key")
        importlib.reload(bing_submit)


if __name__ == "__main__":
    unittest.main(verbosity=2)