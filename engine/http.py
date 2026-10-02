"""Shared async HTTP client with retries, timeouts and a polite UA."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from .ratelimit import TokenBucket

log = logging.getLogger("pseo.http")

USER_AGENT = (
    "Mozilla/5.0 (compatible; LongTailDataAtlasBot/1.0; "
    "+https://example.com/bot) data-page-generator"
)


class Http:
    def __init__(
        self,
        concurrency: int = 8,
        timeout: int = 30,
        retries: int = 3,
        requests_per_second: float = 4.0,
    ) -> None:
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._bucket = TokenBucket(requests_per_second)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = max(1, retries)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Http":
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"},
            connector=aiohttp.TCPConnector(limit=32, ttl_dns_cache=300),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> Any:
        return await self._request("GET", url, params=params, headers=headers)

    async def post_json(self, url: str, payload: Any, headers: dict | None = None) -> Any:
        return await self._request("POST", url, json_body=payload, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_body: Any = None,
        headers: dict | None = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("Http must be used as an async context manager")
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                await self._bucket.acquire()
                async with self._sem:
                    async with self._session.request(
                        method, url, params=params, json=json_body, headers=headers
                    ) as resp:
                        if resp.status == 429:
                            body = (await resp.text())[:300]
                            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                            wait = retry_after or 8.0 * (attempt + 1)
                            await asyncio.sleep(wait)
                            raise aiohttp.ClientError(f"HTTP 429 rate limited: {body}")
                        if resp.status >= 400:
                            body = (await resp.text())[:300]
                            raise aiohttp.ClientError(f"HTTP {resp.status}: {body}")
                        resp.raise_for_status()
                        ctype = resp.headers.get("Content-Type", "")
                        if "json" in ctype:
                            return await resp.json()
                        text = await resp.text()
                        if not text.strip():
                            # Some APIs answer a successful POST with 200 and an empty
                            # body. IndexNow does exactly this. Treating that as a parse
                            # failure turns every accepted submission into a retry and
                            # then an error, so an empty body is returned as-is.
                            return None
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"non-JSON response from {url}: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - deliberate catch-all + retry
                last = exc
                wait = min(30.0, (2 ** attempt) * 1.5)
                log.warning("request failed (%s/%s) %s -> %s", attempt + 1, self._retries, url, exc)
                if attempt < self._retries - 1:
                    await asyncio.sleep(wait)
        # Keep the last error text: callers classify failures (bad key, rate limit,
        # quota exhaustion) from the message, so it must survive the retry wrapper.
        raise RuntimeError(
            f"request failed after {self._retries} attempts: {url} ({last})"
        ) from last


def cache_path(cache_dir: Path, name: str) -> Path:
    path = Path(cache_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_json_cache(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_cache(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def jsonl(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path