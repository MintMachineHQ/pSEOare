"""City climate pages from NASA POWER climatology (key-free, long-term averages)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..http import Http
from ..models import Link, Page, slugify
from .base import cached_fetch, fmt_num, load_asset, trim_to_budget

log = logging.getLogger("pseo.sources.climate")

API = "https://power.larc.nasa.gov/api/temporal/climatology/point"
PARAMETERS = "T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,WS2M,ALLSKY_SFC_SW_DWN"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


async def collect(cfg, http: Http, budget) -> list[Page]:
    opts = cfg.source("climate")
    cities = load_asset(cfg.paths.cache, cfg.paths.assets, "cities.json", "cities")
    limit = int(opts.get("cities", 40))
    stamp = datetime.now(timezone.utc).isoformat()
    selected = cities[:limit]

    async def one(city: dict) -> Page | None:
        try:
            data, from_cache = await cached_fetch(
                http,
                cfg.paths.cache,
                f"climate_{slugify(city['name'])}.json",
                API,
                {
                    "parameters": PARAMETERS,
                    "community": "AG",
                    "longitude": city["lon"],
                    "latitude": city["lat"],
                    "format": "JSON",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("climate %s failed: %s", city["name"], exc)
            return None
        return _build_page(city, data, cfg, stamp, from_cache) if isinstance(data, dict) else None

    results = await asyncio.gather(*(one(c) for c in selected), return_exceptions=True)
    pages: list[Page] = []
    for res in results:
        if isinstance(res, Page):
            pages.append(res)
        elif isinstance(res, Exception):
            log.warning("climate task failed: %s", res)

    pages.sort(key=lambda p: p.slug)
    return trim_to_budget(pages, budget)


def _build_page(city: dict, data: dict, cfg, stamp: str, from_cache: bool) -> Page | None:
    params = ((data.get("properties") or {}).get("parameter")) or {}
    tavg = params.get("T2M") or {}
    tmax = params.get("T2M_MAX") or {}
    tmin = params.get("T2M_MIN") or {}
    rain = params.get("PRECTOTCORR") or {}
    if not tavg or "ANN" not in tavg:
        return None

    rows = []
    for month in MONTHS:
        if month not in tavg:
            continue
        rows.append(
            [
                month.capitalize(),
                fmt_num(tmin.get(month), 1),
                fmt_num(tavg.get(month), 1),
                fmt_num(tmax.get(month), 1),
                fmt_num(rain.get(month), 2),
            ]
        )

    warmest = max(MONTHS, key=lambda m: tavg.get(m, -999))
    coldest = min(MONTHS, key=lambda m: tavg.get(m, 999))
    wettest = max(MONTHS, key=lambda m: rain.get(m, -999))
    driest = min(MONTHS, key=lambda m: rain.get(m, 999))
    name = city["name"]

    annual_mm = (rain.get("ANN") or 0) * 365
    summary = (
        f"{name}, {city['country']} long-term climate: mean temperature {fmt_num(tavg['ANN'], 1)} C "
        f"per year, warmest month {warmest.capitalize()} at {fmt_num(tavg.get(warmest), 1)} C, coldest "
        f"month {coldest.capitalize()} at {fmt_num(tavg.get(coldest), 1)} C, and about "
        f"{fmt_num(annual_mm, 0)} mm of rain a year ({fmt_num(rain['ANN'], 2)} mm per day on average)."
    )

    return Page(
        kind="climate_city",
        title=f"{name} average monthly temperature and rainfall (long-term climate)",
        h1=f"{name} climate: monthly averages",
        slug=slugify(f"{name}-average-monthly-temperature-rainfall"),
        summary=summary,
        schema_type="Dataset",
        keywords=[
            f"{name} average monthly temperature",
            f"{name} climate",
            f"{name} rainfall by month",
            f"{name} warmest month",
        ],
        facts=[
            ("Annual mean temperature", f"{fmt_num(tavg['ANN'], 1)} C"),
            ("Warmest month", f"{warmest.capitalize()} ({fmt_num(tavg.get(warmest), 1)} C)"),
            ("Coldest month", f"{coldest.capitalize()} ({fmt_num(tavg.get(coldest), 1)} C)"),
            ("Wettest month", f"{wettest.capitalize()} ({fmt_num(rain.get(wettest), 2)} mm/day)"),
            ("Annual precipitation", f"{fmt_num(annual_mm, 0)} mm / year"),
            ("Coordinates", f"{city['lat']}, {city['lon']}"),
            ("Population", fmt_num(city.get("population", 0), 0)),
        ],
        breadcrumbs=[
            Link("Home", cfg.url_for("index.html")),
            Link("Climate data", cfg.url_for("hub-climate.html")),
            Link(name, "#"),
        ],
        data={
            "table": {
                "caption": f"Average monthly temperature and rainfall in {name}",
                "headers": ["Month", "Mean low (C)", "Mean (C)", "Mean high (C)", "Rain (mm/day)"],
                "rows": rows,
            },
            "sections": [
                {
                    "heading": f"What the {name} climate table shows",
                    "body": (
                        "Values are multi-decadal climatological averages for the "
                        f"{city['lat']}, {city['lon']} grid cell, not a single year. Rain is the daily "
                        "mean, so a value of 2.00 mm/day is about 60 mm across a 30-day month. "
                        "Averages smooth over extremes, so use them for planning rather than for "
                        "what to pack for next Tuesday."
                    ),
                }
            ],
            "key": f"climate:{slugify(name)}",
            "from_cache": from_cache,
        },
    )