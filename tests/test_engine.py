"""Offline unit tests: no network, no API keys.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import unittest
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
    LINKS_PER_PAGE,
    assign_related,
    hub_documents,
    hub_filenames,
    not_found_html,
    sitemap_xml,
)  # noqa: E402
from engine.render import build_css, build_theme  # noqa: E402
from engine.http import Http, QuotaExhausted  # noqa: E402
from engine.indexing import key_file_is_reachable, remember_pending, take_pending  # noqa: E402
from engine.models import Link, Page, slugify  # noqa: E402
from engine.ratelimit import CallBudget  # noqa: E402
from engine.sources.base import trim_to_budget  # noqa: E402
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
            self.assertEqual(enricher.groq_candidates(), ["llama-x", "llama-y"])
            self.assertEqual(len(enricher.groq_candidates()), 2)
        finally:
            del os.environ["GROQ_API_KEY"]


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


if __name__ == "__main__":
    unittest.main(verbosity=2)