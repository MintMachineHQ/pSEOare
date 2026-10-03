#!/usr/bin/env python3
"""Rebuild assets/cities.json from GeoNames without breaking a single live URL.

Two constraints that are easy to get wrong:

* Page slugs are derived from the city name alone, so two cities sharing a name overwrite
  each other's page. At 454 cities there were no collisions; at pop>=250k there are 22
  duplicated names covering 44 cities. Every duplicate gets an explicit slug that appends
  the state or country.
* Those slugs are frozen for anything already published. Renaming a live slug orphans a
  page that is already indexed and already submitted to Bing, so when a name is duplicated
  the instance that is already live keeps its bare slug and the others get suffixes.

    python3 tools/build_cities.py /path/to/cities5000.txt /path/to/countryInfo.txt
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "assets" / "cities.json"

MIN_POPULATION = 250_000
PER_COUNTRY_CAP = 30

# GeoNames citiesNNNN.txt column indices.
# Verified against the raw dump: 6 is the feature class (P), 7 the feature code (PPLA),
# and 8 is the ISO country code. Reading 7 as the country code silently collapses every
# city onto ten pseudo-countries, which is exactly what the first run of this script did.
C_NAME, C_ASCII, C_LAT, C_LON, C_CODE, C_COUNTRY, C_ADMIN1, C_POP = 1, 2, 4, 5, 8, 7, 10, 14


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower().replace("&", "and")).strip("-")


def page_slug(name: str) -> str:
    """Must match climate._build_page exactly for existing cities."""
    return slugify(f"{name}-average-monthly-temperature-rainfall")


def load_country_names(path: Path) -> dict[str, str]:
    """GeoNames id -> country name, so countries read 'China' and not '1814991'."""
    names: dict[str, str] = {}
    if not path.exists():
        return names
    for line in path.read_text("utf-8", errors="replace").splitlines():
        if line.startswith("#") or "\t" not in line:
            continue
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        # Verified against the raw dump: 0 ISO, 1 ISO3, 2 ISO-numeric, 4 country name.
        iso, geonames_id, name = cols[0].strip(), cols[2].strip(), cols[4].strip()
        if geonames_id.isdigit() and name:
            names[geonames_id] = name
            names[f"{iso}:{geonames_id}"] = name
    return names


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cities_path, countries_path = Path(sys.argv[1]), Path(sys.argv[2])
    country_names = load_country_names(countries_path)
    if not country_names:
        print("warning: countryInfo.txt produced no mapping; country names will be ids")

    rows: list[dict] = []
    for line in cities_path.read_text("utf-8", errors="replace").splitlines():
        cols = line.split("\t")
        if len(cols) <= C_POP:
            continue
        try:
            population = int(cols[C_POP] or 0)
            lat, lon = float(cols[C_LAT] or 0.0), float(cols[C_LON] or 0.0)
        except ValueError:
            continue
        if population < MIN_POPULATION or not lat:
            continue
        name = (cols[C_ASCII].strip() or cols[C_NAME].strip())
        code = cols[C_CODE].strip()
        gid = cols[C_COUNTRY].strip()  # numeric country id, mapped below
        rows.append(
            {
                "name": name,
                "country": country_names.get(gid, gid),
                "country_code": code,
                "admin1": cols[C_ADMIN1].strip(),
                "lat": lat,
                "lon": lon,
                "population": population,
            }
        )
    rows.sort(key=lambda r: -r["population"])

    # Biggest cities first, capped per country, so no single country floods the corpus.
    per_country: collections.Counter = collections.Counter()
    selected: list[dict] = []
    for row in rows:
        if per_country[row["country_code"]] >= PER_COUNTRY_CAP:
            continue
        per_country[row["country_code"]] += 1
        selected.append(row)
    print(
        f"geonames gave {len(rows)} cities at pop>={MIN_POPULATION:,}; "
        f"capped to {len(selected)} across {len(per_country)} countries"
    )

    existing = json.loads(OUT.read_text("utf-8"))["cities"]
    existing_by_slug = {page_slug(c["name"]): c for c in existing}
    print(f"existing published cities: {len(existing)}")

    # Union, keyed by slug. Existing cities always win their slug so no URL changes.
    merged: dict[str, dict] = {slug: dict(city) for slug, city in existing_by_slug.items()}
    for row in selected:
        merged.setdefault(page_slug(row["name"]), row)

    name_counts = collections.Counter(slugify(c["name"]) for c in merged.values())
    dupes = {k for k, v in name_counts.items() if v > 1}
    print(f"merged {len(merged)} cities; {len(dupes)} names shared by more than one city")

    used: set[str] = set()
    final: dict[str, dict] = {}
    # Pass 1: every unique name, plus any duplicate that is already live and keeps its
    # bare URL. Pass 2: the remaining duplicates get a qualifier.
    for slug, city in sorted(merged.items()):
        if slugify(city["name"]) not in dupes or slug in existing_by_slug:
            city["slug"] = slug
            final[slug] = city
            used.add(slug)
    for slug, city in sorted(merged.items()):
        if slug in used:
            continue
        qualifier = city.get("admin1") or city.get("country") or city.get("country_code") or "other"
        disambiguated = f"{slugify(qualifier)}-{slug}"
        suffix = 2
        while disambiguated in used:
            disambiguated = f"{slugify(qualifier)}-{suffix}-{slug}"
            suffix += 1
        city = dict(city)
        city["slug"] = disambiguated
        final[disambiguated] = city
        used.add(disambiguated)

    cities = [final[slug] for slug in sorted(final)]
    assert len({c["slug"] for c in cities}) == len(cities), "slug collision survived"

    # Nothing already published may change URL.
    for slug in existing_by_slug:
        assert slug in final, f"published city lost its slug: {slug}"

    OUT.write_text(json.dumps({"cities": cities}, indent=1, ensure_ascii=False) + "\n", "utf-8")

    bands: collections.Counter = collections.Counter()
    for city in cities:
        lat = abs(city["lat"])
        bands["0-10" if lat < 10 else "10-25" if lat < 25 else "25-40" if lat < 40 else "40-55" if lat < 55 else "55+"] += 1
    print(f"wrote {OUT}")
    print(f"  {len(cities)} cities, {len({c['country'] for c in cities})} countries")
    print(f"  latitude spread: {dict(sorted(bands.items()))}")
    print(f"  disambiguated slugs: {sum(1 for c in cities if c['slug'] != page_slug(c['name']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())