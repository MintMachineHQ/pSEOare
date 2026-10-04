"""Derived metrics per page, accumulated into a store the explore pages are built from.

Rankings, comparisons, the Today page, the JSON API and the widgets all need to compare
things *across* the whole corpus. The generator only ever builds the slice of pages it
collects on a given run -- a few hundred out of ~1,700 -- so none of those pages could be
produced from the pages in hand. They are produced from this store instead.

Every page that a run builds contributes its derived figures here, and the store is keyed
by path, so it accumulates towards covering the whole corpus and converges. A page that
has never been built simply does not appear, which is why a ranking can legitimately list
40 cities on one day and 600 a month later.

Keeping the metrics rather than rebuilding them from prose keeps every derived surface
consistent: the ranking, the comparison, the widget and the API endpoint cannot disagree
about the same city, because they all read one number written once.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("pseo.explore")

STORE = "metrics.json"
# A city missing a usable figure must not silently fall to the bottom of a ranking with a
# nonsense value, so incomplete rows are dropped rather than coerced.
MIN_CLIMATE_ROWS = 6
MIN_CRYPTO_ROWS = 6


@dataclass
class Metrics:
    path: str
    kind: str
    title: str
    slug: str = ""
    entity: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetricsStore:
    """Path-keyed store of derived figures, persisted as one JSON file."""

    def __init__(self, cache_dir: Path) -> None:
        self.path = Path(cache_dir) / STORE
        self.rows: dict[str, Metrics] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for path, row in (raw.get("pages") or {}).items():
                    self.rows[path] = Metrics(
                        path=row["path"],
                        kind=row["kind"],
                        title=row["title"],
                        slug=row.get("slug", ""),
                        entity=row.get("entity", ""),
                        data=row.get("data") or {},
                    )
            except (ValueError, KeyError, OSError) as exc:
                # A corrupt store must never fail a build. Rankings rebuild from whatever
                # the next run contributes.
                log.warning("metrics store unreadable, starting empty: %s", exc)
                self.rows = {}

    def record(self, page: Any, derived: dict[str, Any] | None) -> None:
        """Store one page's derived figures, if it produced any."""
        if not derived:
            return
        self.rows[page.path] = Metrics(
            path=page.path,
            kind=page.kind,
            title=page.h1,
            slug=page.slug,
            entity=str(page.data.get("entity") or ""),
            data=derived,
        )

    def save(self) -> None:
        payload = {
            "updated_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "pages": {path: row.to_dict() for path, row in sorted(self.rows.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # -- typed accessors -------------------------------------------------------

    def of_kind(self, *kinds: str) -> list[Metrics]:
        return [r for r in self.rows.values() if r.kind in kinds]

    def climates(self) -> list[Metrics]:
        return [r for r in self.of_kind("climate_city") if "annual_mean" in r.data]

    def cryptos(self) -> list[Metrics]:
        return [r for r in self.of_kind("crypto_12m") if "range_pct" in r.data]

    def countries(self) -> list[Metrics]:
        """Country rows, preferring the profile page and de-duplicating by entity."""
        best: dict[str, Metrics] = {}
        for row in self.of_kind("country_profile", "country_population"):
            key = row.entity or row.slug
            if key not in best or row.kind == "country_profile":
                best[key] = row
        return list(best.values())

    def holidays(self) -> list[Metrics]:
        return [r for r in self.of_kind("holiday_single") if "month" in r.data]


# A page's h1 carries its page type, which must not leak into a name used in a ranking
# row, a comparison heading, a JSON field or a widget title. "Kemerovo climate" in a
# list of cities reads as a mistake, and "Algorand price by month vs Arbitrum price by
# month" reads as a stutter.
_NAME_SUFFIXES = (
    " climate: monthly averages",
    " price by month: 12 month high, low and average",
    " price by month",
    " country data",
)


def _entity_name(page: Any) -> str:
    h1 = str(page.h1)
    for suffix in _NAME_SUFFIXES:
        if h1.endswith(suffix):
            return h1[: -len(suffix)].strip()
    if ":" in h1:
        return h1.split(":")[0].strip()
    return h1.strip()


def metrics_for(page: Any) -> dict[str, Any] | None:
    """Derive the comparable figures for one page, or None when it has no dataset.

    Only comparable figures belong here. A field that exists on one kind and not another
    would make rankings silently lopsided, so each kind gets its own closed set.
    """
    kind = page.kind
    facts = {str(k).strip().lower(): str(v) for k, v in getattr(page, "facts", [])}
    rows = ((page.data.get("table") or {}).get("rows")) or []
    base = {"name": _entity_name(page)}

    if kind == "climate_city":
        return _climate_metrics(page, rows, facts, base)
    if kind == "crypto_12m":
        return _crypto_metrics(page, rows, facts, base)
    if kind in ("country_profile", "country_population", "country_rank"):
        return _country_metrics(facts, base)
    if kind == "holiday_single":
        return _holiday_metrics(page, facts, base)
    return None


def _num(text: str) -> float | None:
    import re

    match = re.search(r"-?[\d,]+(?:\.\d+)?", str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _climate_metrics(
    page: Any, rows: list[Any], facts: dict[str, str], base: dict[str, Any]
) -> dict[str, Any] | None:
    parsed: list[tuple[str, float, float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            parsed.append((str(row[0]), float(row[1]), float(row[2])))
        except (TypeError, ValueError):
            continue
    if len(parsed) < MIN_CLIMATE_ROWS:
        return None
    warmest = max(parsed, key=lambda r: r[1])
    coldest = min(parsed, key=lambda r: r[1])
    wettest = max(parsed, key=lambda r: r[2])
    driest = min(parsed, key=lambda r: r[2])
    annual_mean = _num(facts.get("annual mean temperature", ""))
    if annual_mean is None:
        annual_mean = sum(r[1] for r in parsed) / len(parsed)
    total_rain = sum(r[2] for r in parsed) * 30.4
    return {
        **base,
        "annual_mean": round(annual_mean, 1),
        "warmest_month": warmest[0],
        "warmest": round(warmest[1], 1),
        "coldest_month": coldest[0],
        "coldest": round(coldest[1], 1),
        "swing": round(warmest[1] - coldest[1], 1),
        "wettest_month": wettest[0],
        "wettest_rain": round(wettest[2], 2),
        "driest_month": driest[0],
        "driest_rain": round(driest[2], 2),
        "annual_rain": round(total_rain, 0),
        "months": [[m, round(t, 1), round(r, 2)] for m, t, r in parsed],
    }


def _crypto_metrics(
    page: Any, rows: list[Any], facts: dict[str, str], base: dict[str, Any]
) -> dict[str, Any] | None:
    high = _num(facts.get("12 month high", ""))
    low = _num(facts.get("12 month low", ""))
    last = _num(facts.get("latest price", ""))
    if high is None or low is None or last is None or high <= low:
        return None
    position = (last - low) / (high - low) * 100
    monthly: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            monthly.append(
                [str(row[0]), round(float(row[1]), 2), round(float(row[2]), 2),
                 round(float(row[3]), 2), round(float(row[4]), 2)]
            )
        except (TypeError, ValueError):
            continue
    return {
        **base,
        "high": high,
        "low": low,
        "last": last,
        "range_pct": round((high - low) / low * 100, 1),
        "position": round(position, 1),
        "above_low_pct": round((last - low) / low * 100, 1),
        "below_high_pct": round((high - last) / high * 100, 1),
        "months": monthly,
    }


_COUNTRY_KEEP = (
    "population", "gdp per capita", "life expectancy", "fertility rate",
    "internet users", "urban population", "surface area", "population growth",
)


def _country_metrics(facts: dict[str, str], base: dict[str, Any]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for key in _COUNTRY_KEEP:
        value = facts.get(key)
        if value:
            number = _num(value)
            if number is not None:
                out[key] = number
    return {**base, **out} if out else None


def _holiday_metrics(
    page: Any, facts: dict[str, str], base: dict[str, Any]
) -> dict[str, Any] | None:
    from datetime import date

    raw = facts.get("date", "")
    if not raw or len(raw) != 10 or raw[4] != "-":
        return None
    try:
        when = date(int(raw[:4]), int(raw[5:7]), int(raw[8:10]))
    except ValueError:
        return None
    country = facts.get("country", "")
    local = facts.get("local name", "")
    return {
        **base,
        "date": when.isoformat(),
        "month": when.month,
        "day": when.day,
        "weekday": when.strftime("%A"),
        "weekend": when.weekday() >= 5,
        "country": country,
        "local_name": local,
        "next_date": date(when.year + 1, when.month, when.day).isoformat(),
    }