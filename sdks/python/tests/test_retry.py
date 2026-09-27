from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

import blazecrawl.client as client_module
from blazecrawl import BlazeCrawl
from blazecrawl.exceptions import RateLimitError, ValidationError


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, path, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class AsyncStubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def request(self, method, path, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def response(status, payload=None):
    return httpx.Response(status, json=payload or {"error": f"status {status}"})


def test_retries_429_and_5xx_before_succeeding(monkeypatch):
    client = BlazeCrawl(max_retries=2, backoff_base=0.5, backoff_jitter=0)
    stub = StubClient(
        [
            response(429),
            response(503),
            response(200, {"success": True, "data": {"markdown": "# ok"}}),
        ]
    )
    sleeps = []
    client._client = stub
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)

    result = client.scrape("https://example.com")

    assert result == {"markdown": "# ok"}
    assert stub.calls == 3
    assert sleeps == [0.5, 1.0]


def test_retry_can_be_disabled(monkeypatch):
    client = BlazeCrawl(max_retries=0)
    stub = StubClient([response(429)])
    client._client = stub
    monkeypatch.setattr(
        client_module.time,
        "sleep",
        lambda _delay: pytest.fail("sleep should not run when retries are disabled"),
    )

    with pytest.raises(RateLimitError):
        client.scrape("https://example.com")

    assert stub.calls == 1


def test_non_transient_4xx_is_not_retried(monkeypatch):
    client = BlazeCrawl(max_retries=3)
    stub = StubClient([response(422, {"error": "invalid"})])
    client._client = stub
    monkeypatch.setattr(
        client_module.time,
        "sleep",
        lambda _delay: pytest.fail("validation failures should not be retried"),
    )

    with pytest.raises(ValidationError):
        client.scrape("https://example.com")

    assert stub.calls == 1


async def test_async_requests_use_same_retry_policy(monkeypatch):
    client = BlazeCrawl(max_retries=1, backoff_base=0.25, backoff_jitter=0)
    stub = AsyncStubClient(
        [
            response(500),
            response(200, {"success": True, "data": {"markdown": "# async"}}),
        ]
    )
    client._async = stub
    sleep = AsyncMock()
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)

    result = await client.ascrape("https://example.com")

    assert result == {"markdown": "# async"}
    assert stub.calls == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_retries": -1}, "max_retries"),
        ({"backoff_base": -0.1}, "backoff_base"),
        ({"backoff_jitter": -0.1}, "backoff_jitter"),
    ],
)
def test_retry_configuration_rejects_negative_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        BlazeCrawl(**kwargs)
