from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from blazecrawl_core.engine.robots import (
    clear_cache,
    get_crawl_delay,
    is_allowed,
)
from blazecrawl_core.network.egress import EgressResponse
from blazecrawl_core.network.ssrf import SSRFValidationError


def _make_response(
    content: str,
    status_code: int = 200,
    url: str = "https://example.com/robots.txt",
) -> EgressResponse:
    return EgressResponse(
        url=url,
        final_url=url,
        status_code=status_code,
        headers={"content-type": "text/plain"},
        content=content.encode("utf-8"),
    )


@pytest.fixture(autouse=True)
def _reset_robots_cache():
    clear_cache()
    yield
    clear_cache()


# ==============================================================================
# 1. Disallow rules coverage
# ==============================================================================


async def test_robots_disallow_root():
    robots_txt = "User-agent: *\nDisallow: /\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/")
        assert not await is_allowed("https://example.com/index.html")
        assert not await is_allowed("https://example.com/api/v1/data")


async def test_robots_disallow_specific_path():
    robots_txt = "User-agent: *\nDisallow: /admin/\nDisallow: /private\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/admin/")
        assert not await is_allowed("https://example.com/admin/dashboard")
        assert not await is_allowed("https://example.com/private")
        assert not await is_allowed("https://example.com/private/data")
        assert await is_allowed("https://example.com/public")
        assert await is_allowed("https://example.com/")


async def test_robots_empty_disallow_allows_all():
    robots_txt = "User-agent: *\nDisallow:\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await is_allowed("https://example.com/")
        assert await is_allowed("https://example.com/admin")
        assert await is_allowed("https://example.com/secret/file.txt")


async def test_robots_allow_overrides_disallow():
    robots_txt = "User-agent: *\nDisallow: /admin/\nAllow: /admin/public/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await is_allowed("https://example.com/admin/public/page")
        assert not await is_allowed("https://example.com/admin/private")


async def test_robots_user_agent_specificity():
    robots_txt = (
        "User-agent: BlazeBot\n"
        "Disallow: /secret/\n\n"
        "User-agent: Googlebot\n"
        "Disallow: /crawler-only/\n\n"
        "User-agent: *\n"
        "Disallow: /global-block/\n"
    )
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        # BlazeBot obeys BlazeBot section
        assert not await is_allowed("https://example.com/secret/", user_agent="BlazeBot")
        assert await is_allowed("https://example.com/global-block/", user_agent="BlazeBot")

        # OtherBot falls back to * section
        assert await is_allowed("https://example.com/secret/", user_agent="OtherBot")
        assert not await is_allowed("https://example.com/global-block/", user_agent="OtherBot")


async def test_robots_path_case_sensitivity():
    robots_txt = "User-agent: *\nDisallow: /secret/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/secret/")
        assert await is_allowed("https://example.com/SECRET/")


async def test_robots_query_parameters():
    robots_txt = "User-agent: *\nDisallow: /search?\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/search?q=test")
        assert await is_allowed("https://example.com/search")


# ==============================================================================
# 2. Wildcards coverage
# ==============================================================================


async def test_robots_wildcard_user_agent_matching():
    robots_txt = "User-agent: *\nDisallow: /hidden/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/hidden/", user_agent="*")
        assert not await is_allowed("https://example.com/hidden/", user_agent="CustomAgent/1.0")
        assert await is_allowed("https://example.com/visible/", user_agent="CustomAgent/1.0")


async def test_robots_wildcard_disallow_all():
    robots_txt = "User-agent: *\nDisallow: *\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert not await is_allowed("https://example.com/")
        assert not await is_allowed("https://example.com/anything")


# ==============================================================================
# 3. Crawl-delay presence coverage
# ==============================================================================


async def test_robots_crawl_delay_integer_presence():
    robots_txt = "User-agent: *\nCrawl-delay: 5\nDisallow: /blocked/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await get_crawl_delay("https://example.com/") == 5.0
        assert await is_allowed("https://example.com/allowed")
        assert not await is_allowed("https://example.com/blocked/")


async def test_robots_crawl_delay_per_user_agent():
    robots_txt = (
        "User-agent: FastBot\n"
        "Crawl-delay: 1\n"
        "Disallow: /private/\n\n"
        "User-agent: SlowBot\n"
        "Crawl-delay: 10\n\n"
        "User-agent: *\n"
        "Crawl-delay: 3\n"
    )
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await get_crawl_delay("https://example.com/", user_agent="FastBot") == 1.0
        assert await get_crawl_delay("https://example.com/", user_agent="SlowBot") == 10.0
        assert await get_crawl_delay("https://example.com/", user_agent="OtherBot") == 3.0


async def test_robots_crawl_delay_non_digit_ignored():
    robots_txt = "User-agent: *\nCrawl-delay: invalid\nDisallow: /restricted/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await get_crawl_delay("https://example.com/") is None
        assert not await is_allowed("https://example.com/restricted/")
        assert await is_allowed("https://example.com/open")


async def test_robots_crawl_delay_absent():
    robots_txt = "User-agent: *\nDisallow: /admin/\n"
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(robots_txt),
    ):
        assert await get_crawl_delay("https://example.com/") is None


# ==============================================================================
# 4. Unreachable-robots fallback path coverage
# ==============================================================================


async def test_robots_unreachable_404_status():
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response("Not Found", status_code=404),
    ):
        assert await is_allowed("https://example.com/anything") is True
        assert await get_crawl_delay("https://example.com/") is None


async def test_robots_unreachable_500_status():
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response("Internal Server Error", status_code=500),
    ):
        assert await is_allowed("https://example.com/anything") is True
        assert await get_crawl_delay("https://example.com/") is None


async def test_robots_unreachable_network_exception():
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        side_effect=ConnectionResetError("Connection reset by peer"),
    ):
        assert await is_allowed("https://example.com/anything") is True
        assert await get_crawl_delay("https://example.com/") is None


async def test_robots_malformed_content_fallback():
    malformed_robots = (
        "<!DOCTYPE html><html><body>\n"
        "Some random web page served instead of robots.txt\n"
        "\x00\x01\x02\n"
        "</body></html>"
    )
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        return_value=_make_response(malformed_robots),
    ):
        assert await is_allowed("https://example.com/page") is True


async def test_robots_ssrf_validation_error_propagates():
    with patch(
        "blazecrawl_core.engine.robots.safe_fetch",
        new_callable=AsyncMock,
        side_effect=SSRFValidationError("DNS rebinding blocked"),
    ):
        with pytest.raises(SSRFValidationError):
            await is_allowed("https://127.0.0.1/robots.txt")

        with pytest.raises(SSRFValidationError):
            await get_crawl_delay("https://127.0.0.1/robots.txt")


async def test_robots_cache_behavior():
    robots_txt = "User-agent: *\nDisallow: /cached-block/\n"
    fetch_mock = AsyncMock(return_value=_make_response(robots_txt))
    with patch("blazecrawl_core.engine.robots.safe_fetch", fetch_mock):
        assert not await is_allowed("https://example.com/cached-block/")
        assert not await is_allowed("https://example.com/cached-block/sub")
        assert await get_crawl_delay("https://example.com/") is None
        # Verify safe_fetch was only called once due to origin caching
        assert fetch_mock.call_count == 1

        # Clear cache and verify subsequent call re-fetches
        clear_cache()
        assert not await is_allowed("https://example.com/cached-block/")
        assert fetch_mock.call_count == 2
