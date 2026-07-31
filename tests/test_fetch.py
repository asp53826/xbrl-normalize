import time

import httpx
import pytest

from xbrl_normalize.fetch import FetchError, Fetcher, Pacer

UA = "xbrl-normalize-tests tests@example.com"


def mk(handler, tmp_path, **kw):
    return Fetcher(cache_dir=tmp_path / "c", user_agent=UA,
                   transport=httpx.MockTransport(handler), **kw)


async def test_contact_address_required(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    with pytest.raises(FetchError, match="contact address"):
        Fetcher(cache_dir=tmp_path)
    with pytest.raises(FetchError):
        Fetcher(cache_dir=tmp_path, user_agent="anonymous-bot")


async def test_pacer_does_not_burst():
    """A token bucket seeded full would release `capacity` instantly and then
    sustain the rate on top, exceeding the limit it claims to enforce."""
    import asyncio

    p = Pacer(rate=50.0)
    stamps = []

    async def one():
        await p.acquire()
        stamps.append(time.monotonic())

    await asyncio.gather(*(one() for _ in range(40)))
    worst = max(sum(1 for s in stamps if t <= s < t + 1.0) for t in stamps)
    assert worst <= 50


async def test_second_read_is_served_from_cache(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"cik": 1, "facts": {}})

    async with mk(handler, tmp_path) as f:
        await f.company_facts(320193)
        await f.company_facts(320193)
    assert calls["n"] == 1
    assert f.hits == 1 and f.downloads == 1


async def test_expired_ttl_refetches(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"cik": 1})

    async with mk(handler, tmp_path, ttl=0.0) as f:
        await f.company_facts(1)
        await f.company_facts(1)
    assert calls["n"] == 2


async def test_retries_then_succeeds(tmp_path):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async with mk(handler, tmp_path) as f:
        assert await f.company_facts(1) == {"ok": True}
    assert n["i"] == 3


async def test_404_is_not_retried(tmp_path):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(404)

    async with mk(handler, tmp_path) as f:
        with pytest.raises(FetchError, match="no data"):
            await f.company_facts(999999)
    assert n["i"] == 1


async def test_corrupt_cache_entry_is_refetched(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"cik": 7})

    async with mk(handler, tmp_path) as f:
        await f.company_facts(7)
        f._path(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{7:010d}.json").write_text("{ broken")
        assert await f.company_facts(7) == {"cik": 7}


async def test_ticker_map_is_uppercased(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"0": {"cik_str": 320193, "ticker": "aapl",
                                               "title": "Apple Inc."}})

    async with mk(handler, tmp_path) as f:
        assert (await f.tickers())["AAPL"] == 320193
