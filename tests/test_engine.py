"""Offline unit tests: no network, no API keys.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Config  # noqa: E402
from engine.enrich import (  # noqa: E402
    FALLBACK_MODELS,
    Enricher,
    _is_hard_failure,
    clean,
    fallback_copy,
    prose_and_faq,
)
from engine.hubs import assign_related, sitemap_xml  # noqa: E402
from engine.models import Link, Page, slugify  # noqa: E402
from engine.render import build_css, build_theme, pick_copy, render_page, waterfall_js  # noqa: E402
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


class TestModelResolution(unittest.TestCase):
    def test_fallback_models_are_current(self):
        self.assertNotIn("1.5", " ".join(FALLBACK_MODELS))
        self.assertIn("gemini-2.0-flash", FALLBACK_MODELS)

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