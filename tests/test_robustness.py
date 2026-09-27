"""Robustness and concurrency tests for BlazeCrawl Core.

Tests malformed inputs, concurrent requests, and edge cases.
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BLAZECRAWL_AUTH_DISABLED", "true")
os.environ.setdefault("BLAZECRAWL_HOST", "127.0.0.1")

from blazecrawl_core.api.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_concurrent_health_checks(client):
    """Test API handles concurrent health checks without errors."""
    import concurrent.futures

    def check_health():
        resp = client.get("/health")
        return resp.status_code

    # Run 10 concurrent health checks
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: check_health(), range(10)))

    # All should succeed
    assert all(status == 200 for status in results)


def test_malformed_json_request(client):
    """Test API rejects malformed JSON gracefully."""
    resp = client.post(
        "/v1/scrape",
        content=b'{"url": "https://example.com", invalid json}',
        headers={"Content-Type": "application/json"},
    )
    # Should return 4xx error, not crash
    assert resp.status_code in [400, 422]


def test_invalid_url_scheme(client):
    """Test API rejects invalid URL schemes."""
    resp = client.post("/v1/scrape", json={"url": "javascript:alert(1)"})
    # Should reject non-http(s) schemes
    assert resp.status_code in [400, 422]


def test_missing_required_fields(client):
    """Test API validates required fields."""
    resp = client.post("/v1/scrape", json={})
    assert resp.status_code == 422


def test_empty_url(client):
    """Test API rejects empty URL."""
    resp = client.post("/v1/scrape", json={"url": ""})
    assert resp.status_code == 422


def test_null_url(client):
    """Test API rejects null URL."""
    resp = client.post("/v1/scrape", json={"url": None})
    assert resp.status_code == 422


def test_oversized_url(client):
    """Test API handles extremely long URLs."""
    long_url = "https://example.com/" + "a" * 10000
    resp = client.post("/v1/scrape", json={"url": long_url})
    # Should either accept or reject gracefully (not crash)
    assert resp.status_code in [200, 400, 422, 413]


def test_unicode_url(client):
    """Test API handles Unicode URLs correctly."""
    resp = client.post("/v1/scrape", json={"url": "https://例え.jp/テスト"})
    # Should handle IDN/Unicode domains (may fail on network, but shouldn't crash)
    assert resp.status_code in [200, 400, 422, 500]


def test_concurrent_scrape_requests(client):
    """Test API handles concurrent scrape requests without race conditions."""
    import concurrent.futures

    def scrape():
        resp = client.post("/v1/scrape", json={"url": "https://example.com", "timeout_ms": 5000})
        return resp.status_code

    # Run 5 concurrent scrapes
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: scrape(), range(5)))

    # Should not crash (may timeout or fail on network, but no exceptions)
    for result in results:
        assert result in [200, 400, 401, 403, 422, 500, 502, 504]


def test_url_normalization_edge_cases():
    """Test URL normalizer handles edge cases."""
    from blazecrawl_core.engine.url_normalizer import get_url_normalizer

    normalizer = get_url_normalizer()

    # Test cases that should not crash
    edge_cases = [
        "",
        "not-a-url",
        "http://",
        "https://example.com:99999",
        "https://example.com/" + "x" * 1000,
        "https://user:pass@example.com",
        "ftp://example.com",
        "file:///etc/passwd",
    ]

    for url in edge_cases:
        try:
            result = normalizer.normalize(url)
            # Should return None or a normalized URL, not crash
            assert result is None or isinstance(result, str)
        except Exception as e:
            pytest.fail(f"URL normalizer crashed on '{url}': {e}")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://EXAMPLE.COM:443/docs/", "https://example.com/docs"),
        ("http://Example.com:80/path", "http://example.com/path"),
        ("https://example.com:8443/path", "https://example.com:8443/path"),
        ("https://example.com/docs#install", "https://example.com/docs"),
        (
            "https://example.com/?utm_source=test&b=2&a=1",
            "https://example.com?a=1&b=2",
        ),
        ("https://example.com/%7Euser", "https://example.com/~user"),
        ("https://user:secret@example.com/path", "https://example.com/path"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a///b//c", "https://example.com/a/b/c"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        ("https://example.com/?flag", "https://example.com?flag"),
        (
            "https://example.com/?z=2&z=1&a=3",
            "https://example.com?a=3&z=1&z=2",
        ),
    ],
)
def test_url_normalization_additional_edge_cases(url, expected):
    """Cover canonicalization details that are security/cache-key sensitive."""
    from blazecrawl_core.engine.url_normalizer import get_url_normalizer

    assert get_url_normalizer().normalize(url) == expected


@pytest.mark.anyio
async def test_html_extraction_malformed_input():
    """Test HTML extractor handles malformed input."""
    from blazecrawl_core.engine.content_extractor import ExtractOptions, get_content_extractor

    extractor = get_content_extractor()

    malformed_inputs = [
        "",  # Empty
        "<html>",  # Unclosed tag
        "<html><body><div",  # Incomplete
        "\x00\x01\x02",  # Binary garbage
        "<script>alert(1)</script>",  # XSS attempt
    ]

    for html in malformed_inputs:
        try:
            # Should handle gracefully, not crash
            result = await extractor.extract(html, "https://example.com", ExtractOptions())
            # Result should have markdown attribute
            assert hasattr(result, "markdown")
        except Exception as e:
            pytest.fail(f"Extractor crashed on malformed input: {e}")


@pytest.mark.anyio
async def test_robots_txt_malformed():
    """Test robots.txt parser handles malformed content."""
    from blazecrawl_core.engine.robots import is_allowed

    # These should not crash, even if robots.txt is malformed
    test_cases = [
        "https://example.com",
        "https://example.com/",
        "https://example.com/path",
    ]

    for url in test_cases:
        try:
            # Should return True or False, not crash
            result = await is_allowed(url, "TestBot")
            assert isinstance(result, bool)
        except Exception as e:
            pytest.fail(f"Robots check crashed on '{url}': {e}")
