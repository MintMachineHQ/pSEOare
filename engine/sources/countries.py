"""Country pages from the World Bank Open Data API (key-free, stable).

One metadata call plus a handful of bulk indicator calls produce country profile
and population/density pages such as "Population of Bhutan".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..http import Http
from ..models import Link, Page, slugify
from .base import cached_fetch, fmt_num, trim_to_budget

log = logging.getLogger("pseo.sources.countries")

COUNTRIES = "https://api.worldbank.org/v2/country"
INDICATOR = "https://api.worldbank.org/v2/country/all/indicator/{code}"

POPULATION = "SP.POP.TOTL"
AREA = "AG.SRF.TOTL.K2"
GDP = "NY.GDP.MKTP.CD"
GDP_PC = "NY.GDP.PCAP.CD"
INDICATORS = {
    POPULATION: "Population, total",
    AREA: "Surface area (sq. km)",
    GDP: "GDP (current US$)",
    GDP_PC: "GDP per capita (current US$)",
}
INDICATOR_YEAR = 2023
MAX_COUNTRIES = 200


def _year_for(indicator: str) -> int:
    return INDICATOR_YEAR - 5 if indicator == AREA else INDICATOR_YEAR


async def collect(cfg, http: Http, budget) -> list[Page]:
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        meta, meta_cached = await cached_fetch(
            http, cfg.paths.cache, "wb_countries.json", COUNTRIES, {"format": "json", "per_page": 400}
        )
    except Exception as exc:  # noqa: BLE001
        log.error("world bank metadata failed: %s", exc)
        return []

    records = _rows(meta)
    sovereign = [
        r for r in records
        if r.get("region", {}).get("id") not in ("", "NA", "Aggregates")
    ]
    sovereign.sort(key=lambda r: r.get("name", ""))
    if not sovereign:
        log.error("world bank metadata produced no usable countries")
        return []

    values: dict[str, dict[str, float]] = {}
    cached_flags: dict[str, bool] = {}
    for code in INDICATORS:
        try:
            payload, cached = await cached_fetch(
                http,
                cfg.paths.cache,
                f"wb_{code}_{_year_for(code)}.json",
                INDICATOR.format(code=code),
                {"format": "json", "per_page": 400, "date": f"{_year_for(code)}:{_year_for(code)}"},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("indicator %s failed: %s", code, exc)
            continue
        cached_flags[code] = cached
        for row in _rows(payload):
            iso3 = row.get("countryiso3code") or ""
            value = row.get("value")
            if iso3 and isinstance(value, (int, float)):
                values.setdefault(iso3, {})[code] = value

    pages: list[Page] = []
    for record in sovereign[:MAX_COUNTRIES]:
        iso3 = record.get("id") or ""
        name = record.get("name") or iso3
        country_values = values.get(iso3, {})
        pages.extend(_country_pages(record, country_values, cfg, stamp, cached_flags.get(POPULATION, meta_cached)))

    pages.extend(_ranking_pages(sovereign, values, cfg, stamp, cached_flags.get(POPULATION, meta_cached)))
    pages.sort(key=lambda p: p.slug)
    return trim_to_budget(pages, budget)


def _rows(payload) -> list[dict]:
    """World Bank answers with [meta, rows]; tolerate a bare rows list too."""
    if not isinstance(payload, list):
        return []
    if len(payload) == 2 and isinstance(payload[1], list):
        return [r for r in payload[1] if isinstance(r, dict)]
    return [r for r in payload if isinstance(r, dict) and "page" not in r]


def _money(value: float | None) -> str:
    if not value:
        return "n/a"
    if value >= 1e12:
        return f"${value / 1e12:.2f} trillion"
    if value >= 1e9:
        return f"${value / 1e9:.2f} billion"
    if value >= 1e6:
        return f"${value / 1e6:.2f} million"
    return f"${value:,.0f}"


def _country_pages(record: dict, v: dict[str, float], cfg, stamp: str, from_cache: bool) -> list[Page]:
    name = record.get("name") or "Unknown"
    iso3 = record.get("id") or ""
    population = v.get(POPULATION)
    area = v.get(AREA)
    gdp = v.get(GDP)
    gdp_pc = v.get(GDP_PC)
    density = (population / area) if population and area else None
    capital = record.get("capitalCity") or "n/a"
    region = (record.get("region") or {}).get("value", "n/a").strip()
    income = (record.get("incomeLevel") or {}).get("value", "n/a")
    lat, lon = record.get("latitude"), record.get("longitude")

    profile = Page(
        kind="country_profile",
        title=f"{name} country data: population, GDP, capital and income group",
        h1=f"{name} country data",
        slug=slugify(f"{name}-country-data"),
        summary=(
            f"{name} ({iso3}), {region}: population {population:,.0f}, surface area "
            f"{area:,.0f} km2, GDP {_money(gdp)}, GDP per capita {_money(gdp_pc)}. "
            f"Capital {capital}; World Bank income group {income}. "
            f"Figures are {INDICATOR_YEAR} World Bank estimates refreshed {stamp[:10]}."
            if population and area
            else f"{name} ({iso3}), {region}: capital {capital}, income group {income}."
        ),
        schema_type="Country",
        keywords=[f"{name} GDP", f"{name} population", f"{name} income group", f"{name} capital"],
        facts=[
            ("ISO3", iso3),
            ("Population", f"{population:,.0f}" if population else "n/a"),
            ("Surface area", f"{area:,.0f} km2" if area else "n/a"),
            ("GDP", _money(gdp)),
            ("GDP per capita", _money(gdp_pc)),
            ("Capital", capital),
            ("Income group", income),
            ("Region", region),
        ],
        breadcrumbs=[
            Link("Home", cfg.url_for("index.html")),
            Link("Country data", cfg.url_for("hub-countries.html")),
            Link(name, "#"),
        ],
        data={
            "entity": name,
            "sections": [
                {
                    "heading": f"Reading the {name} profile",
                    "body": (
                        f"{name} sits in {region} and is classified as {income} by the World Bank. "
                        f"GDP per capita of {_money(gdp_pc)} divides GDP of {_money(gdp)} by the "
                        f"population of {population:,.0f} people. Coordinates in the source record: "
                        f"{lat}, {lon}."
                        if population and area and gdp_pc
                        else f"{name} is in {region}, capital {capital}, income group {income}."
                    ),
                }
            ],
            "key": f"country:{iso3.lower()}",
            "from_cache": from_cache,
        },
    )

    population_page = Page(
        kind="country_population",
        title=f"Population of {name}: {population:,.0f} ({INDICATOR_YEAR} estimate)"
        if population
        else f"Population of {name}",
        h1=f"Population of {name}",
        slug=slugify(f"population-of-{name}"),
        summary=(
            f"{name} has {population:,.0f} people across {area:,.0f} km2 of land, a density of "
            f"{fmt_num(density, 1)} people per square kilometre. GDP per capita is {_money(gdp_pc)} "
            f"and the capital is {capital}. World Bank {INDICATOR_YEAR} data, refreshed {stamp[:10]}."
            if population and area
            else f"{name} population record from the World Bank open dataset, refreshed {stamp[:10]}."
        ),
        schema_type="Dataset",
        keywords=[f"population of {name}", f"{name} population density", f"{name} population {INDICATOR_YEAR}"],
        facts=[
            ("Population", f"{population:,.0f}" if population else "n/a"),
            ("Surface area", f"{area:,.0f} km2" if area else "n/a"),
            ("Density", f"{fmt_num(density, 1)} /km2" if density else "n/a"),
            ("Capital", capital),
            ("Income group", income),
            ("Data year", str(INDICATOR_YEAR)),
        ],
        breadcrumbs=[
            Link("Home", cfg.url_for("index.html")),
            Link("Country data", cfg.url_for("hub-countries.html")),
            Link(name, "#"),
        ],
        data={
            "entity": name,
            "sections": [
                {
                    "heading": f"How the {name} population figure is derived",
                    "body": (
                        f"Population and land area come from the same World Bank release, so the "
                        f"density of {fmt_num(density, 1)} people per km2 is internally consistent. "
                        f"Cross-check against the country ranking table to see where {name} sits."
                        if density
                        else "Land area was not available for this country in the chosen release."
                    ),
                }
            ],
            "key": f"country_pop:{iso3.lower()}",
            "from_cache": from_cache,
        },
    )
    return [profile, population_page]


def _ranking_pages(sovereign: list[dict], values: dict, cfg, stamp: str, from_cache: bool) -> list[Page]:
    ranked = sorted(
        (r for r in sovereign if (values.get(r.get("id", ""), {}) or {}).get(POPULATION)),
        key=lambda r: -values[r["id"]][POPULATION],
    )[:30]
    if not ranked:
        return []
    rows = []
    for i, record in enumerate(ranked):
        v = values.get(record["id"], {})
        population = v.get(POPULATION)
        area = v.get(AREA)
        density = population / area if population and area else None
        rows.append(
            [
                str(i + 1),
                record.get("name", ""),
                f"{population:,.0f}",
                f"{area:,.0f}" if area else "n/a",
                fmt_num(density, 1) if density else "n/a",
            ]
        )
    return [
        Page(
            kind="country_rank",
            title="30 most populated countries with area and population density",
            h1="Most populated countries",
            slug=slugify("most-populated-countries"),
            summary=(
                "Ranked table of the 30 most populated countries with surface area and population "
                f"density per km2, built from World Bank {INDICATOR_YEAR} data on {stamp[:10]}."
            ),
            schema_type="Dataset",
            keywords=["most populated countries", "country population ranking", "population density by country"],
            facts=[
                ("Countries listed", str(len(ranked))),
                ("Source", f"World Bank {INDICATOR_YEAR}"),
                ("Largest population", f"{values[ranked[0]['id']][POPULATION]:,.0f}"),
            ],
            breadcrumbs=[
                Link("Home", cfg.url_for("index.html")),
                Link("Country data", cfg.url_for("hub-countries.html")),
                Link("Population ranking", "#"),
            ],
            data={
            "entity": record.get("name", ""),
                "table": {
                    "caption": "30 most populated countries",
                    "headers": ["Rank", "Country", "Population", "Area (km2)", "Density /km2"],
                    "rows": rows,
                },
                "key": "country_rank",
                "from_cache": from_cache,
            },
        )
    ]