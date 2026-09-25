import asyncio
from unittest.mock import patch

import httpx
import pytest

from blazecrawl import BlazeCrawl, RateLimitError


def _sync_client(handler, **kwargs):
    client = BlazeCrawl(base_url="http://test", **kwargs)
    client._client.close()
    client._client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_sync_requests_retry_429_and_5xx_then_succeed():
    statuses = iter([429, 503, 200])
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        status = next(statuses)
        payload = {"data": {"ok": True}} if status == 200 else {"error": "retry"}
        return httpx.Response(status, json=payload, request=request)

    client = _sync_client(handler, max_attempts=3, backoff_factor=0)
    try:
        assert client.scrape("https://example.com") == {"ok": True}
        assert calls == 3
    finally:
        client.close()


def test_max_attempts_one_disables_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "limited"}, request=request)

    client = _sync_client(handler, max_attempts=1)
    try:
        with patch("blazecrawl.client.time.sleep") as sleep:
            with pytest.raises(RateLimitError):
                client.scrape("https://example.com")
        assert calls == 1
        sleep.assert_not_called()
    finally:
        client.close()


def test_non_transient_client_error_is_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "bad request"}, request=request)

    client = _sync_client(handler, max_attempts=3, backoff_factor=0)
    try:
        with pytest.raises(Exception):
            client.scrape("https://example.com")
        assert calls == 1
    finally:
        client.close()


@pytest.mark.asyncio
async def test_async_requests_use_the_same_retry_policy():
    statuses = iter([500, 200])
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        status = next(statuses)
        payload = {"data": {"ok": True}} if status == 200 else {"error": "retry"}
        return httpx.Response(status, json=payload, request=request)

    client = BlazeCrawl(base_url="http://test", max_attempts=2, backoff_factor=0)
    await client._async.aclose()
    client._async = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.ascrape("https://example.com") == {"ok": True}
        assert calls == 2
    finally:
        await client.aclose()


def test_retry_delay_is_bounded_exponential_with_jitter():
    client = BlazeCrawl(
        base_url="http://test",
        max_attempts=4,
        backoff_factor=0.5,
        max_backoff=1.0,
    )
    try:
        with patch("blazecrawl.client.random.uniform", return_value=0.25) as jitter:
            assert client._retry_delay(0) == 0.25
            assert client._retry_delay(2) == 0.25
        assert jitter.call_args_list[0].args == (0.0, 0.5)
        assert jitter.call_args_list[1].args == (0.0, 1.0)
    finally:
        client.close()
