from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

import blazecrawl.client as client_module
from blazecrawl import BlazeCrawl


class AsyncStubClient:
    def __init__(self, post_response, get_responses=None):
        self.post_response = post_response
        self.get_responses = list(get_responses or [])
        self.post = AsyncMock(side_effect=self._post)
        self.get = AsyncMock(side_effect=self._get)

    async def _post(self, path, json):
        return self.post_response

    async def _get(self, path):
        return self.get_responses.pop(0)


def response(status, payload):
    return httpx.Response(status, json=payload)


@pytest.mark.asyncio
async def test_acrawl_returns_job_immediately_when_wait_is_false():
    client = BlazeCrawl()
    job = {"job_id": "job-1", "status": "queued"}
    stub = AsyncStubClient(response(202, job))
    client._async = stub

    result = await client.acrawl(
        "https://example.com",
        wait=False,
        max_pages=4,
        max_depth=1,
    )

    assert result == job
    stub.post.assert_awaited_once_with(
        "/v1/crawl",
        json={
            "url": "https://example.com",
            "max_pages": 4,
            "max_depth": 1,
        },
    )
    stub.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_acrawl_polls_until_job_finishes(monkeypatch):
    client = BlazeCrawl()
    stub = AsyncStubClient(
        response(202, {"job_id": "job-2", "status": "queued"}),
        [
            response(200, {"job_id": "job-2", "status": "running"}),
            response(
                200,
                {
                    "job_id": "job-2",
                    "status": "completed",
                    "pages_crawled": 3,
                },
            ),
        ],
    )
    client._async = stub
    sleep = AsyncMock()
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)

    result = await client.acrawl("https://example.com", poll_interval=0.25)

    assert result["status"] == "completed"
    assert result["pages_crawled"] == 3
    assert stub.get.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_acrawl_returns_failed_terminal_job_without_extra_poll():
    client = BlazeCrawl()
    stub = AsyncStubClient(
        response(202, {"job_id": "job-3", "status": "queued"}),
        [response(200, {"job_id": "job-3", "status": "failed", "error": "boom"})],
    )
    client._async = stub

    result = await client.acrawl("https://example.com")

    assert result["status"] == "failed"
    assert stub.get.await_count == 1
