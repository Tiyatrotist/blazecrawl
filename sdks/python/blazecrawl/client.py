"""BlazeCrawl Python SDK client."""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

import httpx

from blazecrawl.exceptions import (
    AuthError,
    BlazeCrawlError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)


class BlazeCrawl:
    """Client for a BlazeCrawl Core server.

    Args:
        api_key: API key (falls back to ``BLAZECRAWL_API_KEY``).
        base_url: Server base URL (falls back to ``BLAZECRAWL_API_URL``,
            default ``http://127.0.0.1:8000``).
        timeout: Request timeout in seconds.
        max_retries: Number of retries after the initial request for HTTP 429/5xx.
        backoff_base: Base delay in seconds for exponential retry backoff.
        backoff_jitter: Maximum random jitter in seconds added to each retry delay.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        backoff_jitter: float = 0.1,
    ) -> None:
        self.api_key = api_key or os.environ.get("BLAZECRAWL_API_KEY")
        self.base_url = (
            base_url or os.environ.get("BLAZECRAWL_API_URL") or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.timeout = timeout
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base < 0:
            raise ValueError("backoff_base must be non-negative")
        if backoff_jitter < 0:
            raise ValueError("backoff_jitter must be non-negative")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_jitter = backoff_jitter
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)
        self._async = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    # -- error handling -----------------------------------------------------
    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": resp.text}
        msg = payload.get("detail", {}).get("message") or payload.get("error") or resp.text
        if resp.status_code == 401:
            raise AuthError(msg, resp.status_code, payload)
        if resp.status_code == 404:
            raise NotFoundError(msg, resp.status_code, payload)
        if resp.status_code == 429:
            raise RateLimitError(msg, resp.status_code, payload)
        if resp.status_code in (400, 422):
            raise ValidationError(msg, resp.status_code, payload)
        raise BlazeCrawlError(msg, resp.status_code, payload)

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    def _retry_delay(self, retry_index: int) -> float:
        exponential = self.backoff_base * (2**retry_index)
        jitter = random.uniform(0.0, self.backoff_jitter) if self.backoff_jitter else 0.0
        return exponential + jitter

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            response = self._client.request(method, path, **kwargs)
            if not self._retryable_status(response.status_code) or attempt >= self.max_retries:
                return response
            time.sleep(self._retry_delay(attempt))
        raise AssertionError("unreachable")

    async def _arequest(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            response = await self._async.request(method, path, **kwargs)
            if not self._retryable_status(response.status_code) or attempt >= self.max_retries:
                return response
            await asyncio.sleep(self._retry_delay(attempt))
        raise AssertionError("unreachable")

    # -- sync API -----------------------------------------------------------
    def scrape(self, url: str, **kwargs: Any) -> dict:
        body = {"url": url, **kwargs}
        r = self._request("POST", "/v1/scrape", json=body)
        if r.status_code >= 400:
            self._raise(r)
        return r.json().get("data", {})

    def map(self, url: str, **kwargs: Any) -> dict:
        r = self._request("POST", "/v1/map", json={"url": url, **kwargs})
        if r.status_code >= 400:
            self._raise(r)
        return r.json()

    def crawl(self, url: str, wait: bool = True, poll_interval: float = 1.0, **kwargs: Any) -> dict:
        r = self._request("POST", "/v1/crawl", json={"url": url, **kwargs})
        if r.status_code >= 400:
            self._raise(r)
        job = r.json()
        if not wait:
            return job
        jid = job["job_id"]
        while True:
            g = self._request("GET", f"/v1/crawl/{jid}")
            if g.status_code >= 400:
                self._raise(g)
            st = g.json()
            if st.get("status") in ("completed", "failed", "cancelled"):
                return st
            time.sleep(poll_interval)

    def crawl_status(self, job_id: str) -> dict:
        r = self._request("GET", f"/v1/crawl/{job_id}")
        if r.status_code >= 400:
            self._raise(r)
        return r.json()

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    # -- async API ----------------------------------------------------------
    async def ascrape(self, url: str, **kwargs: Any) -> dict:
        r = await self._arequest("POST", "/v1/scrape", json={"url": url, **kwargs})
        if r.status_code >= 400:
            self._raise(r)
        return r.json().get("data", {})

    async def amap(self, url: str, **kwargs: Any) -> dict:
        r = await self._arequest("POST", "/v1/map", json={"url": url, **kwargs})
        if r.status_code >= 400:
            self._raise(r)
        return r.json()

    async def aclose(self) -> None:
        await self._async.aclose()
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BlazeCrawl:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
