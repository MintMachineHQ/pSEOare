"""Shared helpers for data sources: raw caching, fallback and page budgeting."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ..http import Http, QuotaExhausted, read_json_cache, write_json_cache
from ..models import Page
from ..ratelimit import CallBudget

log = logging.getLogger("pseo.sources")


async def cached_fetch(
    http: Http,
    cache_dir: Path,
    cache_name: str,
    url: str,
    params: dict | None = None,
    fetch: Callable[[str, dict | None], Awaitable[Any]] | None = None,
    propagate_quota: bool = False,
) -> tuple[Any, bool]:
    """Return (payload, from_cache). Falls back to the last good payload on failure.

    Set ``propagate_quota`` when the caller wants to distinguish "this endpoint's daily
    allowance is gone" from a transient failure, so it can stop making calls instead of
    walking the rest of its list into the same wall. The cached payload is still served
    first; the exception is raised after, never instead of it.
    """
    path = cache_dir / cache_name
    marker = path.with_suffix(".unavailable")
    fetcher = fetch or http.get_json
    if marker.exists():
        # Endpoint answered with a permanent error before (unsupported country, 404, ...).
        # Remember it so scheduled runs do not keep burning retries on a known dead path.
        return None, True
    try:
        data = await fetcher(url, params)
        write_json_cache(path, data)
        marker.unlink(missing_ok=True)
        return data, False
    except Exception as exc:  # noqa: BLE001
        cached = read_json_cache(path)
        if cached is None and _is_permanent(exc):
            # Only a definitive refusal earns the permanent marker. Writing it for a
            # rate limit or a daily quota would blacklist a working endpoint for good,
            # because the marker is checked before every later run and never expires.
            log.error("fetch permanently unavailable (%s); not retrying: %s", url, exc)
            try:
                marker.write_text(str(exc), encoding="utf-8")
            except OSError:
                pass
        if cached is not None:
            log.warning("fetch failed (%s); using cached %s", exc, cache_name)
            if propagate_quota and isinstance(exc, QuotaExhausted):
                raise QuotaExhausted(str(exc)) from exc
            return cached, True
        log.error("fetch failed and no cache for %s: %s", url, exc)
        if propagate_quota and isinstance(exc, QuotaExhausted):
            raise
        return None, False


def _is_permanent(exc: Exception) -> bool:
    """True only for errors that will still be errors on the next run."""
    if isinstance(exc, QuotaExhausted):
        return False
    text = str(exc)
    if "HTTP 429" in text:
        return False
    for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 410", "HTTP 451"):
        if code in text:
            return True
    return False


def load_asset(cache_dir: Path, assets_dir: Path, name: str, key: str) -> list[dict]:
    payload = read_json_cache(assets_dir / name)
    if isinstance(payload, dict):
        return payload.get(key, [])
    return payload or []


def take(items: Iterable[Any], n: int) -> list[Any]:
    out: list[Any] = []
    for item in items:
        out.append(item)
        if len(out) >= n:
            break
    return out


def trim_to_budget(pages: list[Page], budget: CallBudget) -> list[Page]:
    """Keep pages until the per-run budget is used up, unseen pages first.

    The sort key is ``not is_new`` on purpose. ``is_new`` is True for a page that has
    not been published yet, and False sorts before True, so sorting on ``is_new``
    directly would put the pages we have already published at the front of the queue.
    Every run would then regenerate the same alphabetical head of the list and the
    corpus would never grow past the first page_budget rows.
    """
    ordered = sorted(pages, key=lambda page: (not budget.is_new(page.path), page.slug))
    # Report the split. A source whose candidate list is entirely "seen" is a saturated
    # dataset, and knowing that per source is the only way to tell "the corpus is full"
    # apart from "the budget is being spent on refreshes while unpublished pages wait".
    unseen = sum(1 for page in ordered if budget.is_new(page.path))
    if not pages or unseen < len(ordered):
        log.info(
            "trim: %d candidates, %d unseen, %d already published, budget %d remaining",
            len(ordered),
            unseen,
            len(ordered) - unseen,
            budget.remaining,
        )
    allowed: list[Page] = []
    for page in ordered:
        if not budget.take():
            break
        allowed.append(page)
    return allowed


def fmt_num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"