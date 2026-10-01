"""Trailing-12-month crypto market pages (source: CoinGecko public API, no key).

The free tier only serves the last 365 days, so pages are framed as rolling
12-month summaries with a monthly breakdown rather than all-time history.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from ..http import Http
from ..models import Link, Page, slugify
from .base import cached_fetch, fmt_num, trim_to_budget

log = logging.getLogger("pseo.sources.crypto")

API = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
COINS = {
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "solana": "Solana",
    "ripple": "XRP",
    "cardano": "Cardano",
    "dogecoin": "Dogecoin",
    "polkadot": "Polkadot",
    "chainlink": "Chainlink",
}


async def collect(cfg, http: Http, budget) -> list[Page]:
    opts = cfg.source("crypto")
    coins = [c.lower() for c in opts.get("coins", ["bitcoin", "ethereum"])][:8]
    stamp = datetime.now(timezone.utc).isoformat()
    pages: list[Page] = []

    async def one(coin: str) -> Page | None:
        try:
            data, from_cache = await cached_fetch(
                http,
                cfg.paths.cache,
                f"crypto_{coin}_365d.json",
                API.format(coin=coin),
                {"vs_currency": "usd", "days": 365},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("crypto %s failed: %s", coin, exc)
            return None
        if not isinstance(data, dict) or not data.get("prices"):
            return None
        return _build_page(coin, data, cfg, stamp, from_cache)

    results = await asyncio.gather(*(one(c) for c in coins), return_exceptions=True)
    for res in results:
        if isinstance(res, Page):
            pages.append(res)
        elif isinstance(res, Exception):
            log.warning("crypto task failed: %s", res)

    pages.sort(key=lambda p: p.slug)
    return trim_to_budget(pages, budget)


def _build_page(coin: str, data: dict, cfg, stamp: str, from_cache: bool) -> Page | None:
    prices = [(int(ts), float(v)) for ts, v in data.get("prices", []) if v is not None]
    volumes = {int(ts): float(v) for ts, v in data.get("total_volumes", []) if v is not None}
    if len(prices) < 30:
        return None

    buckets: dict[str, list[float]] = defaultdict(list)
    for ts, value in prices:
        buckets[datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")].append(value)

    rows: list[list[str]] = []
    for month in sorted(buckets):
        series = buckets[month]
        rows.append(
            [
                month,
                fmt_num(min(series), 2),
                fmt_num(max(series), 2),
                fmt_num(sum(series) / len(series), 2),
                fmt_num(series[-1], 2),
            ]
        )

    all_values = [v for _, v in prices]
    first, last = prices[0][1], prices[-1][1]
    change = ((last - first) / first * 100) if first else 0.0
    best_month = max(buckets.items(), key=lambda kv: (sum(kv[1]) / len(kv[1])) - kv[1][0])
    best_month_return = ((sum(best_month[1]) / len(best_month[1])) - best_month[1][0]) / best_month[1][0] * 100
    total_volume = sum(volumes.values())

    name = COINS.get(coin, coin.capitalize())
    summary = (
        f"{name} over the last 12 months: high {fmt_num(max(all_values), 2)} USD, low "
        f"{fmt_num(min(all_values), 2)} USD, first close {fmt_num(first, 2)} USD and latest "
        f"{fmt_num(last, 2)} USD ({change:+.1f}%). Best month {best_month[0]} "
        f"({best_month_return:+.1f}%)."
    )

    return Page(
        kind="crypto_12m",
        title=f"{name} price by month: 12 month high, low and average",
        h1=f"{name} price by month",
        slug=slugify(f"{name}-price-by-month"),
        summary=summary,
        schema_type="Dataset",
        keywords=[
            f"{name.lower()} price by month",
            f"{name.lower()} 12 month high",
            f"{name.lower()} monthly average price",
        ],
        facts=[
            ("12 month high", f"{fmt_num(max(all_values), 2)} USD"),
            ("12 month low", f"{fmt_num(min(all_values), 2)} USD"),
            ("Latest price", f"{fmt_num(last, 2)} USD"),
            ("12 month change", f"{change:+.1f}%"),
            ("Total volume", f"{fmt_num(total_volume / 1e9, 2)} B USD"),
            ("Data points", str(len(prices))),
        ],
        breadcrumbs=[
            Link("Home", cfg.url_for("index.html")),
            Link("Crypto data", cfg.url_for("hub-crypto.html")),
            Link(name, "#"),
        ],
        data={
            "table": {
                "caption": f"{name} monthly price summary in USD (last 12 months)",
                "headers": ["Month", "Low", "High", "Average", "Month-end"],
                "rows": rows,
            },
            "sections": [
                {
                    "heading": f"How this {name} monthly table is built",
                    "body": (
                        "Every row aggregates the daily prices returned by the CoinGecko public "
                        "market_chart endpoint for the last 365 days, which is the longest window "
                        "the key-free tier serves. The month-end column is the last price of that "
                        "month, so the series can be chained into a longer history yourself."
                    ),
                }
            ],
            "key": f"crypto_12m:{coin}",
            "from_cache": from_cache,
        },
    )