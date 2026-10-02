"""Search-engine notification: IndexNow (Bing/Yandex) and optional Google Indexing API."""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import aiohttp

from .config import Config, env_secret
from .http import Http, read_json_cache, write_json_cache

log = logging.getLogger("pseo.indexing")

INDEXNOW = "https://api.indexnow.org/indexnow"
INDEXNOW_BATCH = 100
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/indexing"
GOOGLE_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications"
GOOGLE_MAX_URLS = 100


def key_file_is_reachable(cfg: Config) -> bool:
    """True when the site root equals the published root (no project subpath)."""
    remainder = cfg.domain.replace("https://", "").replace("http://", "").strip("/")
    return "/" not in remainder


PENDING_FILE = "indexnow_pending.json"


def take_pending(cfg: Config) -> list[str]:
    """URLs published by the previous run, which are live by now.

    IndexNow validates every submitted URL and rejects a batch whose members are not
    reachable. A URL created during this run is still missing from the published site,
    because publishing happens after generation, so notifying on the same run would be
    rejected every time. Each run therefore notifies the previous run's new URLs, which
    the last publish has made live.
    """
    return read_json_cache(cfg.paths.cache / PENDING_FILE, []) or []


def remember_pending(cfg: Config, urls: list[str]) -> None:
    """Queue this run's new URLs for the next run to notify."""
    if urls:
        write_json_cache(cfg.paths.cache / PENDING_FILE, urls)


async def notify(cfg: Config, http: Http, new_urls: list[str]) -> dict[str, int]:
    stats = {"indexnow": 0, "google": 0}
    pending = take_pending(cfg)
    if not pending and not new_urls:
        return stats

    inx = cfg.indexing.get("indexnow", {})
    if inx.get("enabled"):
        key = env_secret(inx.get("key_env", "INDEXNOW_KEY"))
        if not key:
            log.warning("indexnow enabled but no key configured; skipping")
        else:
            stats["indexnow"] = await _indexnow(http, cfg, key, pending)
    remember_pending(cfg, [cfg.url_for(path) for path in new_urls])

    gapi = cfg.indexing.get("google_indexing_api", {})
    if gapi.get("enabled"):
        creds = env_secret(gapi.get("credentials_env", "GOOGLE_INDEXING_CREDENTIALS"))
        if not creds:
            log.warning("google indexing enabled but no service-account JSON configured; skipping")
        else:
            stats["google"] = await _google(http, creds, pending[:GOOGLE_MAX_URLS])

    return stats


async def _indexnow(http: Http, cfg: Config, key: str, urls: list[str]) -> int:
    if not key_file_is_reachable(cfg):
        log.warning(
            "IndexNow needs %s/%s.txt at the domain root, but this site is served from the "
            "subpath %s. IndexNow rejects that with 403 UserForbiddedToAccessSite, so pings are "
            "skipped. Publish on Cloudflare Pages (project.pages.dev) or a custom domain to enable it.",
            cfg.host,
            key,
            cfg.domain,
        )
        return 0

    key_file = Path(cfg.paths.output) / f"{key}.txt"
    try:
        key_file.write_text(key, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write IndexNow key file: %s", exc)

    sent = 0
    for start in range(0, len(urls), INDEXNOW_BATCH):
        batch = urls[start : start + INDEXNOW_BATCH]
        payload = {
            "host": cfg.host,
            "key": key,
            "keyLocation": f"{cfg.domain}/{key}.txt",
            "urlList": batch,
        }
        try:
            await http.post_json(INDEXNOW, payload)
            sent += len(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("indexnow batch failed: %s", exc)
            if sent == 0:
                log.warning(
                    "IndexNow validates %s/%s.txt over HTTP. It is published by this run, "
                    "so the first build is expected to fail; later runs succeed.",
                    cfg.domain,
                    key,
                )
    log.info("IndexNow: notified %d urls", sent)
    return sent


async def _google(http: Http, creds_json: str, urls: list[str]) -> int:
    """Opt-in only. Google limits this API to JobPosting/BroadcastEvent resources;
    submitting ordinary content pages through it risks a manual action."""
    try:
        creds = json.loads(creds_json)
        token = await _google_access_token(creds)
    except Exception as exc:  # noqa: BLE001
        log.error("google indexing unavailable: %s", exc)
        return 0

    sent = 0
    async with aiohttp.ClientSession() as session:
        for url in urls:
            body = {
                "url": url,
                "type": "URLNotificationRequest",
                "httpRequest": {
                    "method": "POST",
                    "requestHeaders": {"Content-Type": "application/json"},
                },
            }
            try:
                async with session.post(
                    GOOGLE_ENDPOINT,
                    data=json.dumps(body),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    if resp.status >= 400:
                        log.warning("google indexing %s for %s", resp.status, url)
                        continue
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("google indexing failed for %s: %s", url, exc)
    log.info("google indexing: submitted %d urls", sent)
    return sent


async def _google_access_token(creds: dict) -> str:
    """Service-account JWT bearer flow, so no google-auth dependency is needed."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is required for the Google Indexing API") from exc

    now = int(time.time())

    def segment(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": creds["client_email"],
        "scope": GOOGLE_SCOPE,
        "aud": GOOGLE_TOKEN,
        "exp": now + 3600,
        "iat": now,
    }
    signing_input = f"{segment(header)}.{segment(claims)}".encode()
    private_key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GOOGLE_TOKEN,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return data["access_token"]


__all__ = ["notify"]