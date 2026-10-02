"""Public holidays by country and year (source: Nager.Date, no API key)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from ..http import Http
from ..models import Link, Page, slugify
from .base import cached_fetch, load_asset, trim_to_budget

log = logging.getLogger("pseo.sources.holidays")

API = "https://date.nager.at/api/v3/PublicHolidays/{year}/{code}"
MAX_HOLIDAY_PAGES_PER_COUNTRY = 3


def _year_window(cfg_years_back: int, cfg_years_forward: int) -> list[int]:
    today = date.today().year
    start = today - max(0, cfg_years_back)
    end = today + max(0, cfg_years_forward)
    return list(range(start, end + 1))


async def collect(cfg, http: Http, budget) -> list[Page]:
    opts = cfg.source("holidays")
    countries = load_asset(cfg.paths.cache, cfg.paths.assets, "countries.json", "countries")
    years = _year_window(int(opts.get("years_back", 1)), int(opts.get("years_forward", 1)))
    pages: list[Page] = []
    stamp = datetime.now(timezone.utc).isoformat()

    async def one(country: dict, year: int) -> list[Page]:
        code = country["code"]
        name = country["name"]
        try:
            data, from_cache = await cached_fetch(
                http,
                cfg.paths.cache,
                f"holidays_{code}_{year}.json",
                API.format(year=year, code=code),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("holidays %s %s failed: %s", code, year, exc)
            return []
        if not isinstance(data, list) or not data:
            return []  # unsupported country/year, or permanently unavailable endpoint
        holidays = [h for h in data if isinstance(h, dict)]
        return _build_pages(country, year, holidays, cfg, stamp, from_cache)

    tasks = [one(c, y) for c in countries for y in years]
    for chunk_start in range(0, len(tasks), 12):
        results = await asyncio.gather(*tasks[chunk_start : chunk_start + 12], return_exceptions=True)
        for res in results:
            if isinstance(res, list):
                pages.extend(res)
            elif isinstance(res, Exception):
                log.warning("holidays task failed: %s", res)

    pages.sort(key=lambda p: p.slug)
    return trim_to_budget(pages, budget)


def _build_pages(
    country: dict,
    year: int,
    holidays: list[dict],
    cfg,
    stamp: str,
    from_cache: bool,
) -> list[Page]:
    name = country["name"]
    code = country["code"]
    pages: list[Page] = []
    rows = sorted(holidays, key=lambda h: h.get("date", ""))

    facts = [
        ("Public holidays", str(len(rows))),
        ("Year", str(year)),
        ("Currency", country.get("currency", "n/a").title()),
        ("Main language", country.get("language", "n/a")),
        ("Updated", stamp[:10]),
    ]

    rows_md = "\n".join(
        f"| {h.get('date')} | {h.get('name')} | {'Yes' if h.get('localName') else 'Yes'} |"
        for h in rows
    )

    summary = (
        f"All {len(rows)} public holidays in {name} for {year}, with dates, local names and "
        f"whether each day is a nationwide public holiday. Sourced from the Nager.Date open "
        f"holiday API and verified on {stamp[:10]}."
    )

    pages.append(
        Page(
            kind="holidays_year",
            title=f"Public holidays in {name} in {year} ({len(rows)} days)",
            h1=f"Public holidays in {name} {year}",
            slug=slugify(f"public-holidays-{name}-{year}"),
            summary=summary,
            schema_type="Dataset",
            keywords=[f"public holidays {name}", f"holidays {name} {year}", f"{name} bank holidays {year}"],
            facts=facts,
            breadcrumbs=[Link("Home", cfg.url_for("index.html")), Link(f"{name} holidays", "#")],
            data={
                "table": {
                    "caption": f"Public holidays in {name}, {year}",
                    "headers": ["Date", "Holiday", "Observed"],
                    "rows": [[h.get("date", ""), h.get("name", ""), h.get("localName", "")] for h in rows],
                },
                "sections": [
                    {
                        "heading": f"{name} holiday calendar {year}",
                        "body": rows_md
                        or "No public holidays were returned for this country and year.",
                    }
                ],
                "key": f"holidays:{code}:{year}",
                "from_cache": from_cache,
            },
        )
    )

    # Highest-signal individual holiday pages (national days dominate search demand).
    ranked = sorted(
        rows,
        key=lambda h: 0 if h.get("localName") and h.get("name") in ("New Year's Day", "Christmas Day") else 1,
    )
    for holiday in ranked[:MAX_HOLIDAY_PAGES_PER_COUNTRY]:
        hname = holiday.get("name") or "Holiday"
        local = holiday.get("localName") or ""
        hdate = holiday.get("date") or ""
        pages.append(
            Page(
                kind="holiday_single",
                title=f"{hname} in {name} {year}: date, traditions and next occurrence",
                h1=f"{hname} in {name} ({year})",
                slug=slugify(f"{hname}-{name}-{hdate or year}"),
                summary=(
                    f"{hname} in {name} falls on {hdate} in {year}."
                    + (f" Local name: {local}." if local else "")
                    + f" Region: {holiday.get('counties') and 'some regions' or 'nationwide'}."
                ),
                schema_type="Event",
                keywords=[f"{hname} {name}", f"when is {hname} in {name}"],
                facts=[
                    ("Date", hdate or "n/a"),
                    ("Local name", local or hname),
                    ("Country", name),
                    ("Year", str(year)),
                ],
                breadcrumbs=[
                    Link("Home", cfg.url_for("index.html")),
                    Link(f"{name} holidays", "#"),
                    Link(hname, "#"),
                ],
                data={
                    "facts_only": True,
                    "key": f"holiday:{code}:{year}:{slugify(hname)}",
                    "from_cache": from_cache,
                },
            )
        )
    return pages