"""SEC companyfacts fetcher: paced, cached, and blunt about the User-Agent rule."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC publishes a 10 req/s ceiling; sit under it. A block is sticky.
RATE = 9.0
TTL = 24 * 3600.0


class FetchError(RuntimeError):
    pass


class Pacer:
    """Spaces grants 1/rate apart. A token bucket seeded full would let a burst
    of concurrent fetches exceed the limit it claims to enforce."""

    def __init__(self, rate: float = RATE):
        self.interval = 1.0 / rate
        self.next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self.next_slot > now:
                await asyncio.sleep(self.next_slot - now)
                now = self.next_slot
            self.next_slot = max(now, self.next_slot) + self.interval


class Fetcher:
    def __init__(self, cache_dir: Path | None = None, user_agent: str | None = None,
                 ttl: float = TTL, transport: httpx.AsyncBaseTransport | None = None):
        ua = user_agent or os.environ.get("EDGAR_USER_AGENT")
        if not ua or "@" not in ua:
            raise FetchError(
                'SEC requires a contact address in the User-Agent. Set '
                'EDGAR_USER_AGENT="your-project you@example.com"'
            )
        self.cache = cache_dir or Path.home() / ".cache" / "xbrl-normalize"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.pacer = Pacer()
        self.downloads = 0
        self.hits = 0
        self.bytes_down = 0
        self._client = httpx.AsyncClient(
            headers={"User-Agent": ua, "Accept-Encoding": "gzip"},
            timeout=60.0, follow_redirects=True, transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    def _path(self, url: str) -> Path:
        return self.cache / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    async def get_json(self, url: str) -> dict:
        p = self._path(url)
        # data.sec.gov sends no ETag and no Last-Modified, so freshness is a TTL.
        if p.exists() and (time.time() - p.stat().st_mtime) < self.ttl:
            try:
                self.hits += 1
                return json.loads(p.read_text())
            except ValueError:
                pass

        delay = 1.0
        for attempt in range(4):
            await self.pacer.acquire()
            try:
                r = await self._client.get(url)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise FetchError(f"{url}: {e}") from e
                await asyncio.sleep(delay); delay *= 2
                continue
            if r.status_code == 200:
                self.downloads += 1
                self.bytes_down += len(r.content)
                p.write_bytes(r.content)
                return r.json()
            if r.status_code == 404:
                raise FetchError(f"no data for {url}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                await asyncio.sleep(float(r.headers.get("retry-after", delay)))
                delay *= 2
                continue
            raise FetchError(f"{url}: HTTP {r.status_code}")
        raise FetchError(f"{url}: retries exhausted")

    async def company_facts(self, cik: int) -> dict:
        return await self.get_json(FACTS_URL.format(cik=cik))

    async def tickers(self) -> dict[str, int]:
        raw = await self.get_json(TICKERS_URL)
        return {r["ticker"].upper(): r["cik_str"] for r in raw.values()}
