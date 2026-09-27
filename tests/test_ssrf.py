"""SSRF / egress security tests for the OSS public surface.

These are release-blocking. Every vector must be rejected before any network IO.
"""

import re

import pytest

from blazecrawl_core.network.ip_utils import is_blocked_address, is_private_ip, parse_ip_literal
from blazecrawl_core.network.ssrf import SSRFValidationError, validate_and_pin


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",  # 127.0.0.1 as decimal
        "http://0x7f.0.0.1/",  # hex
        "http://[::1]/",  # IPv6 loopback
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
        "http://10.0.0.5/",  # RFC1918 A
        "http://172.16.0.1/",  # RFC1918 B
        "http://192.168.1.1/",  # RFC1918 C
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[fe80::1]/",  # IPv6 link-local
        "http://[fc00::1]/",  # IPv6 ULA
        "http://100.64.0.1/",  # shared address space
        "http://0.0.0.0/",
        "file:///etc/passwd",  # disallowed scheme
        "ftp://example.com/",  # disallowed scheme
        "http://user:pass@example.com/",  # embedded credentials
        "gopher://example.com/",
    ],
)
async def test_blocked_targets_rejected(url):
    with pytest.raises(SSRFValidationError):
        await validate_and_pin(url)


def test_ip_literal_parsing_legacy_forms():
    assert is_blocked_address(parse_ip_literal("127.0.0.1"))
    assert is_blocked_address(parse_ip_literal("2130706433"))
    assert is_blocked_address(parse_ip_literal("::ffff:127.0.0.1"))
    assert is_blocked_address(parse_ip_literal("169.254.169.254"))
    assert not is_blocked_address(parse_ip_literal("8.8.8.8"))


def test_is_private_ip_hostname_resolution():
    assert is_private_ip("127.0.0.1")
    assert is_private_ip("10.1.2.3")
    assert not is_private_ip("8.8.8.8")


async def test_public_target_allowed():
    # example.com is a real public host; should validate without raising.
    target = await validate_and_pin("https://example.com/")
    assert target.hostname == "example.com"
    assert target.ip
    assert target.scheme == "https"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://10.0.0.5/", "private IP address (10.0.0.5)"),
        ("http://127.0.0.1/", "loopback address (127.0.0.1)"),
        (
            "http://169.254.169.254/latest/meta-data",
            "cloud metadata endpoint (169.254.169.254)",
        ),
        ("http://[fe80::1]/", "link-local address (fe80::1)"),
    ],
)
async def test_blocked_literal_reports_specific_reason(url, expected):
    with pytest.raises(SSRFValidationError, match=re.escape(expected)):
        await validate_and_pin(url)


async def test_blocked_dns_result_reports_specific_reason(monkeypatch):
    async def resolve_private(_hostname, _port):
        return ["10.23.4.5"]

    monkeypatch.setattr("blazecrawl_core.network.ssrf._resolve", resolve_private)

    with pytest.raises(
        SSRFValidationError,
        match=re.escape("private IP address (10.23.4.5)"),
    ):
        await validate_and_pin("https://example.test/")
