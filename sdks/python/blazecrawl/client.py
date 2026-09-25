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
        max_attempts: Maximum attempts for HTTP 429/5xx responses. Set to 1
            to disable retries.
        backoff_factor: Base delay in seconds for exponential retry backoff.
        max_backoff: Maximum retry delay in seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_attempts: int = 3,
        backoff_factor: float = 0.25,
        max_backoff: float = 2.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_factor < 0 or max_backoff < 0:
            raise ValueError("retry delays must be non-negative")
        self.api_key = api_key or os.environ.get("BLAZECRAWL_API_KEY")
        self.base_url = (
            base_url or os.environ.get("BLAZECRAWL_API_URL") or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
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
    def _retryable(resp: httpx.Response) -> bool:
        return resp.status_code == 429 or 500 <= resp.status_code < 600

    def _retry_delay(self, retry_index: int) -> float:
        ceiling = min(self.max_backoff, self.backoff_factor * (2**retry_index))
        return random.uniform(0.0, ceiling) if ceiling > 0 else 0.0

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_attempts):
            resp = self._client.request(method, path, **kwargs)
            if not self._retryable(resp) or attempt == self.max_attempts - 1:
                return resp
            time.sleep(self._retry_delay(attempt))
        raise RuntimeError("retry loop exhausted")

    async def _arequest(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_attempts):
            resp = await self._async.request(method, path, **kwargs)
            if not self._retryable(resp) or attempt == self.max_attempts - 1:
                return resp
            await asyncio.sleep(self._retry_delay(attempt))
        raise RuntimeError("retry loop exhausted")

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
