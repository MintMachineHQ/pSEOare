"""Registry of enabled data sources, ordered by configured priority."""
from __future__ import annotations

import logging

from ..config import Config
from ..http import Http
from ..models import Page
from ..ratelimit import CallBudget

log = logging.getLogger("pseo.sources")

MODULES = {
    "holidays": "holidays",
    "countries": "countries",
    "climate": "climate",
    "crypto": "crypto",
}


def _import(name: str):
    from importlib import import_module

    return import_module(f".{MODULES[name]}", package=__name__)


async def collect_all(cfg: Config, http: Http, budget: CallBudget) -> list[Page]:
    names = [name for name in MODULES if cfg.source_enabled(name)]
    names.sort(key=lambda n: int(cfg.source(n).get("priority", 99)))
    if not names:
        return []

    # Each source gets its own slice of the run budget so one greedy source cannot
    # starve the rest and every category stays represented in the published set.
    total = budget.limit
    share = max(5, total // len(names))
    extra = total - share * len(names)
    shares = {name: share + (1 if i < extra else 0) for i, name in enumerate(names)}

    pages: list[Page] = []
    for name in names:
        sub_budget = CallBudget(shares[name], seen=budget.seen)
        try:
            produced = await _import(name).collect(cfg, http, sub_budget)
            log.info("source %s produced %d/%d pages", name, len(produced), shares[name])
            pages.extend(produced)
            budget.take(len(produced))
        except Exception as exc:  # noqa: BLE001 - one broken source must not kill the run
            log.error("source %s failed: %s", name, exc)
    return pages


__all__ = ["MODULES", "collect_all"]