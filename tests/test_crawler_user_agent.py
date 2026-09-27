from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from blazecrawl_core.api.schemas import CrawlRequest, ScrapeRequest
from blazecrawl_core.engine import cache, crawler, robots
from blazecrawl_core.engine.crawler import CrawlJobState, CrawlManager


def test_crawl_request_uses_blazecrawl_default_user_agent():
    request = CrawlRequest(url="https://example.com")

    assert request.user_agent == "BlazeCrawl/0.1.0"


def test_crawl_request_accepts_custom_user_agent():
    request = CrawlRequest(url="https://example.com", user_agent="ResearchBot/2.4")

    assert request.user_agent == "ResearchBot/2.4"


async def test_crawler_passes_job_user_agent_to_robots(monkeypatch):
    manager = CrawlManager()
    job = CrawlJobState(
        job_id="job-1",
        seed="https://example.com/",
        max_pages=1,
        max_depth=0,
        user_agent="ResearchBot/2.4",
    )
    seen = []

    monkeypatch.setattr(crawler, "validate_and_pin", AsyncMock(return_value=None))

    async def reject_for_test(url, user_agent=robots.DEFAULT_USER_AGENT):
        seen.append((url, user_agent))
        return False

    monkeypatch.setattr(crawler.robots, "is_allowed", reject_for_test)

    await manager._crawl(job)

    assert seen == [("https://example.com/", "ResearchBot/2.4")]
    assert job.pages_crawled == 0


@pytest.mark.parametrize("user_agent", ["", " " * 201])
def test_crawl_request_rejects_invalid_user_agent(user_agent):
    with pytest.raises(ValueError):
        CrawlRequest(url="https://example.com", user_agent=user_agent)


def test_scrape_request_accepts_custom_user_agent():
    request = ScrapeRequest(url="https://example.com", user_agent="ResearchBot/2.4")

    assert request.user_agent == "ResearchBot/2.4"


def test_scrape_cache_key_varies_by_user_agent():
    first = cache.make_key(
        "https://example.com", ["markdown"], True, "auto", "ResearchBot/1.0"
    )
    second = cache.make_key(
        "https://example.com", ["markdown"], True, "auto", "ResearchBot/2.0"
    )

    assert first != second
