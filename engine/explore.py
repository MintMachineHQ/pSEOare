"""Derived surfaces built from the metrics store: rankings, comparisons, Today, API,
widgets and charts.

These are the pages that give a corpus a reason to be revisited. A city reference page
answers one question once; a ranking answers "which are the coldest" and changes when
the data does, a comparison answers "colder than Denver", and the Today page answers
"what is happening now". Each is generated deterministically from
:mod:`engine.metrics`, so the ranking, the comparison, the widget and the API endpoint
can never disagree about the same city.

Nothing here fetches anything. If the store is thin, the pages are short, and that is
visible rather than hidden: every surface prints how many rows it was built from.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from .metrics import Metrics, MetricsStore

log = logging.getLogger("pseo.explore")

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# How many rows a listing shows. Kept modest so the page stays a readable reference
# rather than a data dump, and so a partially-populated store still yields a useful page.
TOP_N = 40
COMPARE_MIN = 4


def _fmt(value: float, suffix: str = "", digits: int = 1) -> str:
    return f"{value:,.{digits}f}{suffix}"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _updated(stamp: str) -> str:
    return stamp[:10]


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

def bar_chart(
    values: list[float],
    labels: list[str],
    *,
    title: str = "",
    unit: str = "",
    width: int = 640,
    height: int = 220,
    colour: str = "#3d6ea8",
) -> str:
    """Inline SVG bar chart.

    Inline rather than a PNG: it needs no build step, no image host and no canvas, it
    scales to any viewport, and it prints as text. The numbers are repeated in the markup
    so the chart is readable to a crawler that ignores the geometry.
    """
    if not values:
        return ""
    peak = max(values) or 1.0
    floor = min(0.0, min(values))
    span = (peak - floor) or 1.0
    pad_left, pad_top, gap = 8, 26, 6
    plot_h = height - pad_top - 24
    slot = (width - pad_left * 2) / len(values)
    bar_w = max(3.0, slot - gap)
    bars = []
    for i, value in enumerate(values):
        bar_h = (value - floor) / span * plot_h
        x = pad_left + i * slot + (slot - bar_w) / 2
        y = pad_top + plot_h - bar_h
        safe = _esc(labels[i]) if i < len(labels) else str(i + 1)
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(1.0, bar_h):.1f}"'
            f' fill="{_esc(colour)}"><title>{safe}: {_fmt(value, unit)}</title></rect>'
        )
    heading = f'<text x="{pad_left}" y="16" font-size="13" fill="#333">{_esc(title)}</text>' if title else ""
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" height="{height}"'
        f' role="img" aria-label="{_esc(title or "chart")}"'
        f' xmlns="http://www.w3.org/2000/svg">{heading}'
        f'<line x1="{pad_left}" y1="{pad_top + plot_h:.1f}" x2="{width - pad_left}"'
        f' y2="{pad_top + plot_h:.1f}" stroke="#ccc"/>{"".join(bars)}</svg>'
    )


# ---------------------------------------------------------------------------
# rankings
# ---------------------------------------------------------------------------

# key -> (slug, title, blurb, unit, higher-is-better)
CLIMATE_RANKINGS: dict[str, tuple[str, str, str, str, bool]] = {
    "coldest-cities": (
        "coldest-cities", "Coldest cities by annual mean temperature",
        "Ranked by the annual mean of the twelve monthly averages, lowest first.", " C", False,
    ),
    "warmest-cities": (
        "warmest-cities", "Warmest cities by annual mean temperature",
        "Ranked by the annual mean of the twelve monthly averages, highest first.", " C", True,
    ),
    "largest-seasonal-swing": (
        "largest-seasonal-swing", "Cities with the largest seasonal temperature swing",
        "The gap between the warmest and coldest month, largest first.", " C", True,
    ),
    "wettest-cities": (
        "wettest-cities", "Wettest cities by annual rainfall",
        "Total rainfall across the twelve months, highest first.", " mm", True,
    ),
    "driest-cities": (
        "driest-cities", "Driest cities by annual rainfall",
        "Total rainfall across the twelve months, lowest first.", " mm", False,
    ),
}

CRYPTO_RANKINGS: dict[str, tuple[str, str, str, str, bool]] = {
    "crypto-near-365-day-low": (
        "crypto-near-365-day-low", "Cryptocurrencies closest to their 365-day low",
        "Latest price as a percentage above the low of the trailing 12 months, lowest first.",
        "%", False,
    ),
    "crypto-near-365-day-high": (
        "crypto-near-365-day-high", "Cryptocurrencies closest to their 365-day high",
        "Latest price as a percentage below the high of the trailing 12 months, lowest first.",
        "%", False,
    ),
    "crypto-largest-range": (
        "crypto-largest-range", "Cryptocurrencies with the largest 365-day range",
        "The 12-month high-to-low spread as a percentage of the low, largest first.",
        "%", True,
    ),
}

COUNTRY_RANKINGS: dict[str, tuple[str, str, str, str, bool]] = {
    "most-populous-countries": (
        "most-populous-countries", "Most populous countries",
        "Total population, highest first.", "", True,
    ),
    "highest-life-expectancy": (
        "highest-life-expectancy", "Countries with the highest life expectancy",
        "Life expectancy at birth in years, highest first.", " years", True,
    ),
    "lowest-fertility-rate": (
        "lowest-fertility-rate", "Countries with the lowest fertility rate",
        "Children per woman, lowest first.", "", False,
    ),
}

# Month-specific climate rankings answer recurring seasonal demand, e.g. "coldest cities
# in January", from the same rows the annual rankings use.
# figure -> (column index in the stored month row, unit suffix, display label)
# The label matters: the column is an int, and the table header needs a name.
SEASONAL_FIGURES = {
    "warmest": (1, " C", "Mean temperature"),
    "coldest": (1, " C", "Mean temperature"),
    "wettest": (2, " mm/day", "Rainfall"),
    "driest": (2, " mm/day", "Rainfall"),
}


def _climate_rows(store: MetricsStore) -> list[Metrics]:
    rows = [r for r in store.climates() if r.data.get("months")]
    # Prefer rows carrying all twelve months: a seasonal ranking cannot be built from a
    # partial table, and a city missing July would otherwise rank as though it had none.
    return [r for r in rows if len(r.data["months"]) >= 12]


def ranking_pages(store: MetricsStore, stamp: str) -> list[tuple[str, str, str]]:
    """Return [(filename, html, kind)] for every ranking this store can support."""
    docs: list[tuple[str, str, str]] = []
    for key, (slug, title, blurb, unit, higher) in CLIMATE_RANKINGS.items():
        figure = key.replace("-cities", "")
        if figure == "largest-seasonal-swing":
            field = "swing"
        elif figure == "wettest":
            field = "annual_rain"
        else:
            field = "annual_mean"
        rows = sorted(
            store.climates(),
            key=lambda r: r.data[field],
            reverse=higher,
        )[:TOP_N]
        if rows:
            docs.append((f"top-{slug}.html", _render_ranking(
                f"top-{slug}", title, blurb, stamp, store, "climate", rows, field, unit), "ranking"))
    for key, (slug, title, blurb, unit, higher) in CRYPTO_RANKINGS.items():
        field = {"crypto-near-365-day-low": "above_low_pct",
                 "crypto-near-365-day-high": "below_high_pct",
                 "crypto-largest-range": "range_pct"}[key]
        rows = sorted(store.cryptos(), key=lambda r: r.data[field], reverse=higher)[:TOP_N]
        if rows:
            docs.append((f"top-{slug}.html", _render_ranking(
                f"top-{slug}", title, blurb, stamp, store, "crypto", rows, field, unit), "ranking"))
    for key, (slug, title, blurb, unit, higher) in COUNTRY_RANKINGS.items():
        field = {"most-populous-countries": "population",
                 "highest-life-expectancy": "life expectancy",
                 "lowest-fertility-rate": "fertility rate"}[key]
        rows = [r for r in store.countries() if field in r.data]
        rows = sorted(rows, key=lambda r: r.data[field], reverse=higher)[:TOP_N]
        if rows:
            docs.append((f"top-{slug}.html", _render_ranking(
                f"top-{slug}", title, blurb, stamp, store, "country", rows, field, unit), "ranking"))
    docs.extend(_seasonal_rankings(store, stamp))
    return docs


def _seasonal_rankings(store: MetricsStore, stamp: str) -> list[tuple[str, str, str]]:
    """Monthly climate rankings, e.g. coldest cities in January."""
    docs: list[tuple[str, str, str]] = []
    rows = _climate_rows(store)
    if len(rows) < COMPARE_MIN:
        return docs
    for month_index in range(12):
        month = MONTH_NAMES[month_index]
        for figure, (column, unit, label) in SEASONAL_FIGURES.items():
            higher = figure == "warmest" or figure == "wettest"
            ranked = sorted(
                rows,
                key=lambda r, c=column, i=month_index: r.data["months"][i][c],
                reverse=higher,
            )[:TOP_N]
            title = f"{figure.title()} cities in {month}"
            slug = f"{figure}-cities-in-{month.lower()}"
            docs.append((f"top-{slug}.html", _render_ranking(
                f"top-{slug}", title,
                f"Ranked by the {month} figure in the monthly table, "
                f"{'highest' if higher else 'lowest'} first.",
                stamp, store, "climate", ranked, column, unit, month=month,
                field_label=label), "ranking"))
    return docs


def _render_ranking(
    slug: str,
    title: str,
    blurb: str,
    stamp: str,
    store: MetricsStore,
    kind: str,
    rows: list[Metrics],
    field: str,
    unit: str,
    month: str = "",
    field_label: str = "",
) -> str:
    """One ranking page as a full HTML document."""
    site = _SITE
    name = site["name"]
    description = f"{title}. {blurb} Updated daily from public data."
    items = []
    digits = 2 if unit == " mm/day" else (0 if unit == "%" else 1)
    for i, row in enumerate(rows, 1):
        if isinstance(field, int):
            # Seasonal rankings rank on one column of the month table rather than on a
            # stored scalar, so the value is read from that column.
            column = field
            source = row.data.get("months") or []
            value = _fmt(source[MONTH_NAMES.index(month)][column], unit, digits) if source else ""
        else:
            raw_value = row.data.get(field)
            value = _fmt(raw_value, unit, digits) if isinstance(raw_value, (int, float)) else ""
        href = link(row.path)
        # The full table is written out, not just the ranked figure, so the page is a
        # usable reference and a crawler reads the numbers without running the chart.
        detail = ""
        if kind == "climate" and row.data.get("months"):
            idx = MONTH_NAMES.index(month) if month else None
            if idx is not None:
                detail = f"{row.data['months'][idx][1]:.1f} C, {row.data['months'][idx][2]:.2f} mm/day"
        items.append(
            f'<tr><td>{i}</td><td><a href="{_esc(link)}">{_esc(row.data.get("name", row.title))}</a></td>'
            f'<td class="num">{_esc(value)}</td><td>{_esc(detail)}</td></tr>'
        )
    rows_html = "".join(items)
    charts = ""
    if kind == "climate" and rows:
        months = [r.data["months"][MONTH_NAMES.index(month)][1] for r in rows] if month else [
            r.data.get("annual_mean", 0) for r in rows
        ]
        charts = bar_chart(
            [m for m in months[:12]], [str(r.data.get("name", ""))[:14] for r in rows[:12]],
            title=f"{month + ' ' if month else ''}{title} (first {min(12, len(rows))})",
            unit=" C",
        )
    body = f"""
<h1>{_esc(title)}</h1>
<p class="lede">{_esc(blurb)}</p>
<p class="meta">Updated: {_esc(_updated(stamp))} &middot; built from {len(rows)} of
 {len(store.rows)} tracked pages.</p>
{charts}
<table><caption>{_esc(title)}</caption>
<thead><tr><th>#</th><th>Name</th><th>{_esc(field_label or str(field).replace("_", " ").title())}</th><th>Detail</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<p class="note">Figures come from public open APIs and are rebuilt on a schedule.
<a href="{link('all-datasets-1.html')}">Browse every dataset</a>.</p>
"""
    return _document(title, description, body, stamp)


# ---------------------------------------------------------------------------
# comparisons
# ---------------------------------------------------------------------------

def _pair_name(row: Metrics) -> str:
    return str(row.data.get("name") or row.title)


def _pair_slug(row: Metrics) -> str:
    return row.slug or row.path.replace(".html", "")


def comparison_pages(store: MetricsStore, stamp: str, limit: int = 60) -> list[tuple[str, str, str]]:
    """Pairwise comparison pages, built from neighbouring rows so the set stays stable.

    Pairing by sorted position rather than every combination means the corpus gains a
    bounded number of new pages per run instead of an O(n squared) explosion, and the
    pairs are ones a reader would plausibly want: neighbouring climates rather than two
    random cities on opposite continents.
    """
    docs: list[tuple[str, str, str]] = []
    climates = sorted(store.climates(), key=lambda r: r.data.get("annual_mean", 0))
    for i in range(len(climates) - 1):
        if len(docs) >= limit:
            break
        a, b = climates[i], climates[i + 1]
        docs.append(_comparison_doc(a, b, "climate", stamp))
    cryptos = sorted(store.cryptos(), key=lambda r: r.data.get("last", 0))
    for i in range(len(cryptos) - 1):
        if len(docs) >= limit:
            break
        a, b = cryptos[i], cryptos[i + 1]
        docs.append(_comparison_doc(a, b, "crypto", stamp))
    return docs


def _comparison_doc(a: Metrics, b: Metrics, kind: str, stamp: str) -> tuple[str, str, str]:
    site = _SITE
    an, bn = _pair_name(a), _pair_name(b)
    slug = f"compare-{_pair_slug(a)}-vs-{_pair_slug(b)}"
    title = f"{an} vs {bn}: {kind} compared"
    if kind == "climate":
        am, bm = a.data.get("annual_mean", 0), b.data.get("annual_mean", 0)
        warmer = an if am >= bm else bn
        cooler = bn if am >= bm else an
        asw, bsw = a.data.get("swing", 0), b.data.get("swing", 0)
        steadier = an if asw <= bsw else bn
        arain, brain = a.data.get("annual_rain", 0), b.data.get("annual_rain", 0)
        wetter = an if arain >= brain else bn
        chart = bar_chart(
            [a.data["months"][i][1] for i in range(12)] + [b.data["months"][i][1] for i in range(12)],
            [m[:3] for m in MONTH_NAMES] + [m[:3] for m in MONTH_NAMES],
            title=f"Monthly mean temperature: {an} then {bn}", unit=" C",
        )
        rows = "".join([
            _cmp_row("Annual mean", f"{am:.1f} C", f"{bm:.1f} C"),
            _cmp_row("Warmest month", f"{a.data['warmest_month']} ({a.data['warmest']:.1f} C)",
                     f"{b.data['warmest_month']} ({b.data['warmest']:.1f} C)"),
            _cmp_row("Coldest month", f"{a.data['coldest_month']} ({a.data['coldest']:.1f} C)",
                     f"{b.data['coldest_month']} ({b.data['coldest']:.1f} C)"),
            _cmp_row("Seasonal swing", f"{asw:.1f} C", f"{bsw:.1f} C"),
            _cmp_row("Annual rainfall", f"{arain:,.0f} mm", f"{brain:,.0f} mm"),
            _cmp_row("Wettest month", f"{a.data['wettest_month']} ({a.data['wettest_rain']:.2f} mm/day)",
                     f"{b.data['wettest_month']} ({b.data['wettest_rain']:.2f} mm/day)"),
        ])
        verdicts = [
            f"{warmer} is the warmer of the two on annual mean.",
            f"{steadier} has the smaller seasonal swing, so its months are more even.",
            f"{wetter} gets more rainfall across the year.",
        ]
    else:
        apct, bpct = a.data.get("above_low_pct", 0), b.data.get("above_low_pct", 0)
        nearer = an if apct >= bpct else bn
        chart = bar_chart(
            [a.data["low"], a.data["last"], a.data["high"],
             b.data["low"], b.data["last"], b.data["high"]],
            [f"{an} low", f"{an} now", f"{an} high",
             f"{bn} low", f"{bn} now", f"{bn} high"],
            title=f"365-day range: {an} and {bn}", unit=" USD",
        )
        rows = "".join([
            _cmp_row("12 month high", f"{a.data['high']:,.2f}", f"{b.data['high']:,.2f}"),
            _cmp_row("12 month low", f"{a.data['low']:,.2f}", f"{b.data['low']:,.2f}"),
            _cmp_row("Latest price", f"{a.data['last']:,.2f}", f"{b.data['last']:,.2f}"),
            _cmp_row("Range as % of low", f"{a.data['range_pct']:.1f}%", f"{b.data['range_pct']:.1f}%"),
            _cmp_row("Above 365-day low", f"{apct:.1f}%", f"{bpct:.1f}%"),
        ])
        verdicts = [
            f"{nearer} is trading closer to its own 365-day high.",
            f"{an}'s 12-month range is {a.data['range_pct']:.1f}% of its low against "
            f"{b.data['range_pct']:.1f}% for {bn}.",
        ]
    body = f"""
<h1>{an} vs {bn}</h1>
<p class="lede">A direct comparison of {an} and {bn}, computed from the same figures
behind their individual pages.</p>
<p class="meta">Updated: {_esc(_updated(stamp))}</p>
{chart}
<table><caption>{_esc(an)} compared with {_esc(bn)}</caption>
<thead><tr><th>Measure</th><th>{_esc(an)}</th><th>{_esc(bn)}</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2>What the numbers mean</h2>
<ul>{"".join(f"<li>{_esc(v)}</li>" for v in verdicts)}</ul>
<p class="note">Individual pages:
<a href="{link(a.path)}">{_esc(an)}</a> &middot;
<a href="{link(b.path)}">{_esc(bn)}</a>.</p>
"""
    return f"{slug}.html", _document(title, f"{an} compared with {bn} on climate and price figures.", body, stamp), "comparison"


def _cmp_row(measure: str, a: str, b: str) -> str:
    return (f"<tr><th scope=\"row\">{_esc(measure)}</th><td>{_esc(a)}</td>"
            f"<td>{_esc(b)}</td></tr>")


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

def today_page(store: MetricsStore, stamp: str, when: date | None = None) -> tuple[str, str, str]:
    """A permanent daily destination: what is happening in the data right now."""
    site = _SITE
    day = when or datetime.now(timezone.utc).date()
    sections: list[str] = []

    todays = [r for r in store.holidays() if r.data.get("date") == day.isoformat()]
    tomorrows = [r for r in store.holidays()
                 if r.data.get("date") == (day + __import__("datetime").timedelta(days=1)).isoformat()]
    upcoming = sorted(
        (r for r in store.holidays() if r.data.get("date", "") > day.isoformat()),
        key=lambda r: r.data["date"],
    )[:12]
    if todays or upcoming:
        def hol_rows(rows: list[Metrics]) -> str:
            return "".join(
                f'<tr><td>{_esc(r.data.get("local_name") or r.data.get("name"))}</td>'
                f'<td>{_esc(r.data.get("country"))}</td>'
                f'<td><a href="{link(r.path)}">{_esc(r.data.get("date"))}</a></td>'
                f'<td>{_esc(r.data.get("weekday"))}</td></tr>'
                for r in rows
            ) or '<tr><td colspan="4">None recorded for this date.</td></tr>'
        sections.append(f"""
<h2>Public holidays</h2>
<h3>Today, {day.isoformat()}</h3>
<table><caption>Holidays falling on {day.isoformat()}</caption>
<thead><tr><th>Holiday</th><th>Country</th><th>Date</th><th>Weekday</th></tr></thead>
<tbody>{hol_rows(todays)}</tbody></table>
<h3>Tomorrow</h3>
<table><caption>Holidays falling on {(day + __import__("datetime").timedelta(days=1)).isoformat()}</caption>
<thead><tr><th>Holiday</th><th>Country</th><th>Date</th><th>Weekday</th></tr></thead>
<tbody>{hol_rows(tomorrows)}</tbody></table>
<h3>Coming up</h3>
<table><caption>Next public holidays on record</caption>
<thead><tr><th>Holiday</th><th>Country</th><th>Date</th><th>Weekday</th></tr></thead>
<tbody>{hol_rows(upcoming)}</tbody></table>""")

    climates = store.climates()
    if len(climates) >= COMPARE_MIN:
        warmest = sorted(climates, key=lambda r: r.data.get("annual_mean", 0), reverse=True)[:10]
        coldest = sorted(climates, key=lambda r: r.data.get("annual_mean", 0))[:10]
        wettest = sorted(climates, key=lambda r: r.data.get("annual_rain", 0), reverse=True)[:10]
        sections.append(f"""
<h2>Climate</h2>
{_mini_table("Warmest cities by annual mean", warmest, "annual_mean", " C")}
{_mini_table("Coldest cities by annual mean", coldest, "annual_mean", " C")}
{_mini_table("Wettest cities by annual rainfall", wettest, "annual_rain", " mm")}""")

    cryptos = store.cryptos()
    if cryptos:
        near_high = sorted(cryptos, key=lambda r: r.data.get("below_high_pct", 999))[:10]
        near_low = sorted(cryptos, key=lambda r: r.data.get("above_low_pct", 999))[:10]
        sections.append(f"""
<h2>Crypto</h2>
{_mini_table("Closest to the 365-day high", near_high, "below_high_pct", "% below")}
{_mini_table("Closest to the 365-day low", near_low, "above_low_pct", "% above")}""")

    fact = _daily_fact(climates, cryptos, day)
    title = f"Today on {site['name']}: {day.isoformat()}"
    body = f"""
<h1>{day.strftime('%A')} {day.isoformat()}</h1>
<p class="lede">What the tracked datasets show for today. This page is rebuilt on every
run, so the figures and the dates move with the data.</p>
<p class="meta">Updated: {_esc(_updated(stamp))} &middot; built from {len(store.rows)} pages.</p>
{fact}
{"".join(sections)}
<p class="note">Rankings, comparisons and per-dataset pages are linked from
<a href="{link("index.html")}">the homepage</a>.</p>
"""
    return "today.html", _document(title, f"What is happening across the datasets on {day.isoformat()}.", body, stamp), "today"


def _mini_table(caption: str, rows: list[Metrics], field: str, unit: str) -> str:
    site = _SITE
    cells = "".join(
        f'<tr><td>{i}</td><td><a href="{link(r.path)}">'
        f'{_esc(r.data.get("name", r.title))}</a></td>'
        f'<td class="num">{_fmt(r.data.get(field, 0), unit, 0 if unit == "%" else 1)}</td></tr>'
        for i, r in enumerate(rows, 1)
    )
    return (f'<table><caption>{_esc(caption)}</caption>'
            f'<thead><tr><th>#</th><th>Name</th><th>Value</th></tr></thead>'
            f'<tbody>{cells}</tbody></table>')


def _daily_fact(climates: list[Metrics], cryptos: list[Metrics], day: date) -> str:
    """One observation derived from the data, for the daily-fact surface."""
    if len(climates) >= COMPARE_MIN:
        swingy = max(climates, key=lambda r: r.data.get("swing", 0))
        steady = min(climates, key=lambda r: r.data.get("swing", 0))
        return (
            f'<p class="fact"><strong>Today&rsquo;s climate fact.</strong> '
            f'{_esc(swingy.data.get("name"))} has the largest seasonal swing in the dataset '
            f'at {swingy.data.get("swing", 0):.1f} C between {swingy.data.get("coldest_month")} '
            f'and {swingy.data.get("warmest_month")}, while {_esc(steady.data.get("name"))} '
            f'stays within {steady.data.get("swing", 0):.1f} C all year.</p>'
        )
    if cryptos:
        coin = max(cryptos, key=lambda r: r.data.get("range_pct", 0))
        return (
            f'<p class="fact"><strong>Today&rsquo;s range fact.</strong> '
            f'{_esc(coin.data.get("name"))} has the widest 365-day range in the dataset at '
            f'{coin.data.get("range_pct", 0):.0f}% of its low.</p>'
        )
    return ""


# ---------------------------------------------------------------------------
# API + widgets
# ---------------------------------------------------------------------------

def api_endpoints(store: MetricsStore, stamp: str) -> list[tuple[str, str]]:
    """[(filename, json)] for the public read-only API.

    One file per dataset, plus a manifest. Cloudflare Pages serves the files statically,
    so this costs nothing and needs no server.
    """
    out: list[tuple[str, str]] = []
    climates = store.climates()
    if climates:
        out.append(("api/climate.json", json.dumps({
            "updated": stamp,
            "count": len(climates),
            "source": _SITE["origin"],
            "results": [
                {"city": r.data.get("name"), "page": link(r.path),
                 "annual_mean_c": r.data.get("annual_mean"),
                 "warmest_month": r.data.get("warmest_month"),
                 "coldest_month": r.data.get("coldest_month"),
                 "swing_c": r.data.get("swing"),
                 "annual_rainfall_mm": r.data.get("annual_rain"),
                 "wettest_month": r.data.get("wettest_month")}
                for r in climates
            ],
        }, indent=1)))
    holidays = store.holidays()
    if holidays:
        out.append(("api/holidays.json", json.dumps({
            "updated": stamp,
            "count": len(holidays),
            "source": _SITE["origin"],
            "results": [
                {"holiday": r.data.get("name"), "country": r.data.get("country"),
                 "local_name": r.data.get("local_name"), "date": r.data.get("date"),
                 "weekday": r.data.get("weekday"),
                 "page": link(r.path)}
                for r in holidays
            ],
        }, indent=1)))
    cryptos = store.cryptos()
    if cryptos:
        out.append(("api/crypto.json", json.dumps({
            "updated": stamp,
            "count": len(cryptos),
            "source": _SITE["origin"],
            "note": "high/low/latest cover the trailing 12 months",
            "results": [
                {"asset": r.data.get("name"), "page": link(r.path),
                 "high_12m": r.data.get("high"), "low_12m": r.data.get("low"),
                 "latest": r.data.get("last"),
                 "range_pct_of_low": r.data.get("range_pct"),
                 "pct_above_low": r.data.get("above_low_pct"),
                 "pct_below_high": r.data.get("below_high_pct")}
                for r in cryptos
            ],
        }, indent=1)))
    return out


def entity_endpoints(store: MetricsStore, stamp: str) -> list[tuple[str, str]]:
    """One JSON file per tracked entity, so a lookup is a plain static GET.

    This is what makes "GET /api/climate/Denver" possible without a server: Cloudflare
    Pages serves a file at a path, so the route *is* a filename. A single aggregate file
    forces every consumer to download the whole dataset to read one city.

    Country folders hold several hundred files, which is why the writer keeps only the
    directory rather than each filename as an orphan-sweep keep entry.
    """
    out: list[tuple[str, str]] = []
    for row in store.climates():
        out.append((f"api/cities/{row.slug}.json", json.dumps({
            "city": row.data.get("name"), "page": link(row.path),
            "annual_mean_c": row.data.get("annual_mean"),
            "warmest_month": row.data.get("warmest_month"),
            "warmest_c": row.data.get("warmest"),
            "coldest_month": row.data.get("coldest_month"),
            "coldest_c": row.data.get("coldest"),
            "seasonal_swing_c": row.data.get("swing"),
            "annual_rainfall_mm": row.data.get("annual_rain"),
            "wettest_month": row.data.get("wettest_month"),
            "wettest_mm_per_day": row.data.get("wettest_rain"),
            "driest_month": row.data.get("driest_month"),
            "driest_mm_per_day": row.data.get("driest_rain"),
            "monthly": [{"month": m[0], "mean_c": m[1], "rain_mm_per_day": m[2]}
                        for m in row.data.get("months", [])],
            "updated": stamp, "source": _SITE["origin"],
        }, indent=1)))
    for row in store.cryptos():
        out.append((f"api/crypto/{row.slug}.json", json.dumps({
            "asset": row.data.get("name"), "page": link(row.path),
            "range_12m": {"high": row.data.get("high"), "low": row.data.get("low"),
                          "latest": row.data.get("last")},
            "position_in_range_pct": row.data.get("position"),
            "pct_above_low": row.data.get("above_low_pct"),
            "pct_below_high": row.data.get("below_high_pct"),
            "range_pct_of_low": row.data.get("range_pct"),
            "monthly": row.data.get("months", []),
            "updated": stamp, "source": _SITE["origin"],
        }, indent=1)))
    by_country: dict[str, list[Metrics]] = {}
    for row in store.holidays():
        country = str(row.data.get("country") or "unknown")
        by_country.setdefault(country, []).append(row)
    for country, rows in by_country.items():
        slug = slugify(country)
        rows = sorted(rows, key=lambda r: str(r.data.get("date", "")))
        out.append((f"api/holidays/{slug}.json", json.dumps({
            "country": country, "page": link(f"hub-single-holidays.html"),
            "count": len(rows),
            "holidays": [{"name": r.data.get("name"), "local_name": r.data.get("local_name"),
                          "date": r.data.get("date"), "weekday": r.data.get("weekday"),
                          "weekend": r.data.get("weekend"), "page": link(r.path)}
                         for r in rows],
            "updated": stamp, "source": _SITE["origin"],
        }, indent=1)))
    return out


def slugify(text: str) -> str:
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-", str(text).lower().strip()).strip("-")
    return slug[:80] or "item"


def api_docs(store: MetricsStore, stamp: str) -> tuple[str, str, str]:
    site = _SITE
    blocks = []
    for name, blurb, example in (
        ("cities", "One request per city. Monthly climate averages for that city.",
         '{\n  "city": "Kazan",\n  "annual_mean_c": 3.1,\n  "warmest_month": "Jul",\n'
         '  "swing_c": 33.3\n}'),
        ("holidays", "Public holidays for one country, or for every tracked country.",
         '{\n  "country": "Cyprus",\n  "local_name": "Χριστούγεννα",\n'
         '  "date": "2027-12-25",\n  "weekday": "Saturday"\n}'),
        ("crypto", "365-day high, low, latest and range position for each tracked asset.",
         '{\n  "asset": "Bitcoin",\n  "high_12m": 124739.81,\n  "low_12m": 58566.09,\n'
         '  "pct_above_low": 44.8\n}'),
    ):
        blocks.append(f"""
<h2 id="{name}">{name.title()} API</h2>
<p>{blurb} No key, no rate limit, no attribution required.</p>
<p>Aggregate: <code>{link('api/' + name + '.json')}</code></p>
<pre><code>{_esc(example)}</code></pre>""")
    rows = []
    for row in sorted(store.climates(), key=lambda r: str(r.data.get("name", "")))[:4]:
        rows.append(f'<tr><td><code>GET {link("api/cities/" + row.slug + ".json")}</code></td>'
                    f'<td>{_esc(row.data.get("name"))}</td></tr>')
    for row in sorted(store.cryptos(), key=lambda r: str(r.data.get("name", "")))[:3]:
        rows.append(f'<tr><td><code>GET {link("api/crypto/" + row.slug + ".json")}</code></td>'
                    f'<td>{_esc(row.data.get("name"))}</td></tr>')
    for row in sorted(store.holidays(), key=lambda r: str(r.data.get("country", "")))[:4]:
        slug = slugify(str(row.data.get("country") or ""))
        rows.append(f'<tr><td><code>GET {link("api/holidays/" + slug + ".json")}</code></td>'
                    f'<td>{_esc(row.data.get("country"))}</td></tr>')
    sample = f"""
<h2>One request, one entity</h2>
<p>No key, no rate limit, no payment. These are static files, so a lookup is a plain GET
and there is nothing to authenticate against.</p>
<table><caption>Per-entity endpoints</caption>
<thead><tr><th>Request</th><th>Returns</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p>Each city, asset and country has one. The slugs match the page filenames, so
<code>/api/cities/kazan.json</code> is the same record behind
<code>{link('kazan-average-monthly-temperature-rainfall.html')}</code>.</p>"""
    body = f"""
<h1>Data API</h1>
<p class="lede">The same figures behind every page on this site, as JSON. Free, key-free
and rebuilt daily.</p>
<p class="meta">Updated: {_esc(_updated(stamp))} &middot;
 {len(store.rows)} pages indexed.</p>
{sample}
{"".join(blocks)}
<h2>Terms</h2>
<ul>
<li>Figures come from public open APIs and are on a best-effort basis; check the source
page before relying on a number.</li>
<li>Attribution is appreciated but not required.</li>
<li>Please cache rather than poll aggressively; the data only changes once a day.</li>
</ul>
"""
    return ("api.html", _document("Data API", "Free JSON access to the climate, holiday and crypto data on this site.", body, stamp), "api")


def widget_pages(store: MetricsStore, stamp: str) -> list[tuple[str, str, str]]:
    """Embeddable iframes. Other sites embedding these become distribution."""
    site = _SITE
    docs: list[tuple[str, str, str]] = []
    climates = sorted(store.climates(), key=lambda r: r.data.get("annual_mean", 0), reverse=True)
    for row in climates[:30]:
        name = str(row.data.get("name", ""))
        months = row.data.get("months") or []
        if not months:
            continue
        slug = f"widget-climate-{row.slug}"
        body = f"""
<div class="widget">
<h1>{_esc(name)}: monthly temperature</h1>
{bar_chart([m[1] for m in months], [m[0][:3] for m in months], title="Mean temperature by month", unit=" C")}
<table><caption>{_esc(name)} monthly averages</caption>
<thead><tr><th>Month</th><th>Mean C</th><th>Rain mm/day</th></tr></thead>
<tbody>{"".join(f"<tr><td>{_esc(m[0])}</td><td>{m[1]:.1f}</td><td>{m[2]:.2f}</td></tr>" for m in months)}</tbody></table>
<p class="attrib">Data by <a href="{link('index.html')}">pSEOare</a></p>
</div>"""
        docs.append((f"{slug}.html", _document(
            f"{name} climate widget",
            f"Embeddable monthly temperature chart for {name}.", body, stamp), "widget"))
    for row in store.cryptos()[:15]:
        name = str(row.data.get("name", ""))
        d = row.data
        body = f"""
<div class="widget">
<h1>{_esc(name)}: 365-day range</h1>
<p>Latest <strong>{_fmt(d.get('last', 0), ' USD', 2)}</strong>,
{d.get('below_high_pct', 0):.1f}% below the 12-month high of {_fmt(d.get('high', 0), ' USD', 2)},
{d.get('above_low_pct', 0):.1f}% above the low of {_fmt(d.get('low', 0), ' USD', 2)}.</p>
{bar_chart([d.get('low', 0), d.get('last', 0), d.get('high', 0)], ['12m low', 'latest', '12m high'], title=f"{name} 365-day range", unit=" USD", colour="#7a5c2e")}
<p class="attrib">Data by <a href="{link('index.html')}">pSEOare</a></p>
</div>"""
        docs.append((f"widget-crypto-{row.slug}.html", _document(
            f"{name} 365-day range widget",
            f"Embeddable 365-day range widget for {name}.", body, stamp), "widget"))
    return docs


def embed_snippet(store: MetricsStore, stamp: str) -> tuple[str, str, str]:
    """The page that tells site owners how to embed a widget."""
    site = _SITE
    samples = []
    for row in sorted(store.climates(), key=lambda r: r.data.get("annual_mean", 0), reverse=True)[:3]:
        samples.append(
            f'<iframe src="{link("widget-climate-" + row.slug + ".html")}" '
            f'width="100%" height="520" loading="lazy" '
            f'style="border:0" title="{_esc(row.data.get("name"))} monthly climate"></iframe>'
        )
    body = f"""
<h1>Embed a widget</h1>
<p class="lede">Every chart on this site can be embedded on another site as an iframe.
No account, no key, no script.</p>
<h2>Climate widget</h2>
<pre><code>{_esc(chr(10).join(samples))}</code></pre>
<p>Each widget links back to its source page and carries a small attribution line.</p>
<h2>Crypto range widget</h2>
<pre><code>{_esc(chr(10).join(f'<iframe src="{link("widget-crypto-" + r.slug + ".html")}" width="100%" height="300" loading="lazy" style="border:0" title="{_esc(r.data.get("name"))} 365-day range"></iframe>' for r in store.cryptos()[:3]))}</code></pre>
<p class="note">Widgets are rebuilt on the same schedule as the rest of the site.</p>
"""
    return "widgets.html", _document("Embeddable widgets", "Embed climate charts and crypto range widgets on your own site.", body, stamp), "widgets"


# ---------------------------------------------------------------------------
# shared document shell
# ---------------------------------------------------------------------------

_SITE = {"name": "pSEOare", "origin": "https://pseoare.pages.dev", "clean": True}


def configure(origin: str, name: str, clean: bool = True) -> None:
    _SITE["origin"] = origin.rstrip("/")
    _SITE["name"] = name
    _SITE["clean"] = clean


def link(path: str) -> str:
    """Absolute URL for a generated path.

    The site is served with extensionless URLs, and Cloudflare 308-redirects the .html
    form. Emitting .html here would give every internal link on every ranking,
    comparison and widget a redirect hop, and a redirect hop is a pageview the crawler
    may not follow.
    """
    clean = str(path).lstrip("/")
    if _SITE["clean"]:
        if clean == "index.html":
            return f"{_SITE['origin']}/"
        if clean.endswith(".html"):
            clean = clean[: -len(".html")]
    return f"{_SITE['origin']}/{clean}"


_STYLE = """
body{font:16px/1.55 system-ui,sans-serif;margin:0;padding:1.2rem;max-width:60rem;color:#1c1c1c}
h1{font-size:1.7rem;margin:0 0 .4rem}h2{font-size:1.2rem;margin:1.6rem 0 .5rem}
.lede{font-size:1.05rem;color:#333;margin:.2rem 0 .6rem}
.meta,.note{color:#666;font-size:.85rem}
.fact{background:#f4f6f9;border-left:4px solid #3d6ea8;padding:.7rem .9rem;margin:1rem 0}
table{border-collapse:collapse;width:100%;margin:.6rem 0 1.2rem;font-size:.92rem}
caption{text-align:left;font-weight:600;padding:.3rem 0}
th,td{border:1px solid #ddd;padding:.34rem .5rem;text-align:left;vertical-align:top}
thead th{background:#f4f4f4}td.num{text-align:right;font-variant-numeric:tabular-nums}
.chart{margin:.6rem 0;background:#fafbfc;border:1px solid #eee;border-radius:4px}
a{color:#2c5c96}code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}
pre{background:#f7f7f7;padding:.7rem;overflow:auto;border-radius:4px}
.widget{font:14px/1.5 system-ui,sans-serif;padding:0}
.widget h1{font-size:1.05rem;margin:0 0 .4rem}
.attrib{font-size:.8rem;color:#666;margin:.4rem 0 0}
@media(prefers-color-scheme:dark){body{background:#14171a;color:#e8e8e8}
th,td{border-color:#333}thead th{background:#22262a}.meta,.note{color:#9aa}
pre,.chart{background:#1b1f23}code{background:#252a2f}.fact{background:#1b2129}}
"""


def _document(title: str, description: str, body: str, stamp: str, bare: bool = False) -> str:
    """Full HTML document.

    Every page gets a canonical, a description and Dataset JSON-LD, because these
    surfaces are meant to be the citable ones. `bare` exists for a genuinely chrome-free
    embed, but nothing uses it: the widget pages were originally built bare and the CI
    sanity gate correctly rejected them for having no h1 and no JSON-LD. A widget is
    still a page, and an indexable one.
    """
    site = _SITE
    payload = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": description,
        "url": link("index.html"),
        "dateModified": stamp,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
    }
    head = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_esc(title)} | {_esc(site["name"])}</title>'
        f'<meta name="description" content="{_esc(description)}">'
        '<link rel="canonical" href="' + link("index.html") + '">'
    )
    if bare:
        head += f'<style>{_STYLE}</style>'
    else:
        head += (
            f'<style>{_STYLE}</style>'
            f'<script type="application/ld+json">{json.dumps(payload)}</script>'
            f'<link rel="alternate" type="application/rss+xml" href="{link("feed.xml")}">'
        )
    return f"<!doctype html><html lang=\"en\"><head>{head}</head><body>{body}</body></html>"

# ---------------------------------------------------------------------------
# search + events + topic feeds
# ---------------------------------------------------------------------------

def search_index(store: MetricsStore, stamp: str) -> list[tuple[str, str]]:
    """A JSON index the homepage search box resolves against, client-side.

    The alternative is a round trip per keystroke, which for a static site means no
    search at all. The index is small enough to inline next to the box: it carries one
    label and one URL per tracked entity.
    """
    entries = []
    for row in store.climates():
        name = str(row.data.get("name") or "")
        entries.append({"q": f"{name} climate", "label": f"{name} — climate averages",
                        "url": link(row.path), "kind": "climate"})
    for row in store.cryptos():
        name = str(row.data.get("name") or "")
        entries.append({"q": f"{name} price", "label": f"{name} — 12-month price range",
                        "url": link(row.path), "kind": "crypto"})
    for row in store.countries():
        name = str(row.data.get("name") or "")
        if name:
            entries.append({"q": name, "label": f"{name} — country data",
                            "url": link(row.path), "kind": "country"})
    seen: set[str] = set()
    unique = []
    for entry in entries:
        key = entry["q"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    unique.sort(key=lambda e: e["q"])
    return [("search-index.json", json.dumps(
        {"updated": stamp, "count": len(unique), "entries": unique}, separators=(",", ":")))]


SEARCH_BOX = """
<form class="ask" id="ask" role="search" autocomplete="off">
  <label for="ask-input">What do you want to know?</label>
  <input id="ask-input" name="q" type="search" placeholder="Denver climate, Bitcoin range, Japan holidays" list="ask-options">
  <datalist id="ask-options"></datalist>
  <button type="submit">Search</button>
  <ul id="ask-results" hidden></ul>
</form>
<script>
(function () {
  var form = document.getElementById('ask'), input = document.getElementById('ask-input'),
      results = document.getElementById('ask-results'), options = document.getElementById('ask-options');
  if (!form || !input) return;
  var index = [];
  fetch('search-index.json').then(function (r) { return r.json(); }).then(function (d) {
    index = d.entries || [];
    options.innerHTML = index.slice(0, 500).map(function (e) {
      var o = document.createElement('option'); o.value = e.q; return o.outerHTML;
    }).join('');
  }).catch(function () {});
  function score(entry, q) {
    var label = entry.q.toLowerCase(), i = label.indexOf(q);
    if (i < 0) return 0;
    // An exact prefix beats a mid-string hit, which beats a match that starts late.
    return (i === 0 ? 1000 : 500 - i) + (entry.label.toLowerCase().startsWith(q) ? 200 : 0);
  }
  function render(q) {
    q = q.trim().toLowerCase();
    if (q.length < 2) { results.hidden = true; return; }
    var hits = [];
    for (var i = 0; i < index.length; i++) {
      var s = score(index[i], q);
      if (s > 0) hits.push([s, index[i]]);
    }
    hits.sort(function (a, b) { return b[0] - a[0]; });
    results.innerHTML = hits.slice(0, 8).map(function (h) {
      return '<li><a href="' + h[1].url + '">' + h[1].label + '</a></li>';
    }).join('');
    results.hidden = hits.length === 0;
  }
  input.addEventListener('input', function () { render(input.value); });
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var q = input.value.trim().toLowerCase(), best = null, bestScore = 0;
    for (var i = 0; i < index.length; i++) {
      var s = score(index[i], q);
      if (s > bestScore) { bestScore = s; best = index[i]; }
    }
    if (best) window.location.href = best.url;
  });
})();
</script>
"""


def event_pages(store: MetricsStore, stamp: str, when: date | None = None) -> list[tuple[str, str, str]]:
    """Date-anchored pages for recurring demand.

    "Public holidays this week" is searched every week of the year and answered by a page
    that is rebuilt from the same holiday rows every run. These are the pages that
    earn a revisit rather than a one-off click.
    """
    site = _SITE
    day = when or datetime.now(timezone.utc).date()
    rows = sorted((r for r in store.holidays() if r.data.get("date")), key=lambda r: r.data["date"])
    docs: list[tuple[str, str, str]] = []
    if not rows:
        return docs

    def table(subset: list[Metrics], caption: str) -> str:
        body = "".join(
            f'<tr><td>{_esc(r.data.get("local_name") or r.data.get("name"))}</td>'
            f'<td>{_esc(r.data.get("country"))}</td>'
            f'<td>{_esc(r.data.get("date"))}</td><td>{_esc(r.data.get("weekday"))}</td>'
            f'<td><a href="{link(r.path)}">detail</a></td></tr>'
            for r in subset
        ) or '<tr><td colspan="5">None recorded.</td></tr>'
        return (f'<table><caption>{_esc(caption)}</caption><thead><tr><th>Holiday</th>'
                f'<th>Country</th><th>Date</th><th>Weekday</th><th></th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    from datetime import timedelta

    windows = [
        ("this-week", day, 7, "Public holidays this week"),
        ("next-week", day + timedelta(days=7), 7, "Public holidays next week"),
        ("this-month", day.replace(day=1), None, "Public holidays this month"),
    ]
    for slug, start, span, title in windows:
        if span:
            end = start + timedelta(days=span - 1)
        else:
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        subset = [r for r in rows if start.isoformat() <= str(r.data["date"]) <= end.isoformat()]
        heading = f"{title} ({start.isoformat()} to {end.isoformat()})"
        body = f"""
<h1>{_esc(heading)}</h1>
<p class="lede">Every public holiday on record between {_esc(start.isoformat())} and
{_esc(end.isoformat())}, rebuilt from the tracked calendars on every run.</p>
<p class="meta">Updated: {_esc(_updated(stamp))} &middot; {len(subset)} holidays in this window.</p>
{table(subset, heading)}
<p class="note">Country-by-country calendars are on the
<a href="{link('hub-single-holidays.html')}">holidays hub</a>, and the same data is
available as JSON at <code>{link('api/holidays.json')}</code>.</p>
"""
        docs.append((f"holidays-{slug}.html", _document(
            heading, f"Every public holiday between {start.isoformat()} and {end.isoformat()}.",
            body, stamp), "event"))

    # Per-month calendars, which is where the durable recurring search volume sits.
    by_month: dict[str, list[Metrics]] = {}
    for row in rows:
        by_month.setdefault(str(row.data["date"])[:7], []).append(row)
    for month, subset in sorted(by_month.items()):
        if len(subset) < 3:
            continue
        title = f"Public holidays in {month}"
        body = f"""
<h1>{_esc(title)}</h1>
<p class="lede">Every public holiday on record for {_esc(month)}, by country.</p>
<p class="meta">Updated: {_esc(_updated(stamp))} &middot; {len(subset)} holidays.</p>
{table(subset, title)}
"""
        docs.append((f"holidays-{month}.html", _document(
            title, f"Public holidays falling in {month}.", body, stamp), "event"))
    return docs


def topic_feeds(store: MetricsStore, stamp: str) -> list[tuple[str, str]]:
    """Per-topic RSS, so a reader or aggregator can follow one dataset.

    A single site-wide feed has to mix climate averages with price ranges, and most
    subscribers want one or the other. Each feed is generated, so it costs nothing.
    """
    from xml.sax.saxutils import escape as xml_escape

    out: list[tuple[str, str]] = []
    groups = (
        ("climate", "Climate updates", store.climates(), "climate_city"),
        ("crypto", "Crypto range updates", store.cryptos(), "crypto_12m"),
        ("holidays", "Holiday updates", store.holidays(), "holiday_single"),
    )
    for slug, title, rows, kind in groups:
        rows = sorted(rows, key=lambda r: str(r.data.get("name", "")))[:100]
        if not rows:
            continue
        items = "".join(
            f"<item><title>{xml_escape(str(r.data.get('name') or r.title))}</title>"
            f"<link>{xml_escape(link(r.path))}</link>"
            f"<guid isPermaLink=\"true\">{xml_escape(link(r.path))}</guid>"
            f"<pubDate>{stamp}</pubDate></item>"
            for r in rows
        )
        feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>{xml_escape(title)} | {_SITE['name']}</title>
<link>{xml_escape(link('index.html'))}</link>
<description>{xml_escape(title)} from {_esc(_SITE['origin'])}</description>
<lastBuildDate>{stamp}</lastBuildDate>
{items}
</channel></rss>
"""
        out.append((f"feed-{slug}.xml", feed))
    return out
