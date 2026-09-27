"""Shared SSRF validation and pin-at-connect helpers for URL fetch surfaces.

DNS-rebinding / TOCTOU defense
==============================
``validate_public_url`` historically resolved the hostname, checked the
resolved IPs against the denylist, and returned them — but no caller pinned the
outbound connection to those validated IPs. The actual fetch re-resolved the
hostname independently, so an attacker controlling DNS could return a public IP
at check-time and a blocked IP (169.254.169.254, 127.0.0.1, 10.x, …) at
use-time.

This module closes that gap *once* in a shared egress layer:

- ``validate_and_pin(url)`` resolves once, validates EVERY returned A/AAAA
  record, and returns a :class:`PinnedTarget` carrying the single IP the caller
  MUST connect to. Callers never re-resolve.
- ``build_pinned_async_client`` (see ``egress.py``) wires that pinned IP into
  an httpx transport so the socket connects to the validated IP while the
  original hostname is preserved for the Host header and TLS SNI.
- ``curl_resolve_entries`` and ``host_resolver_rules`` pin curl_cffi and
  Chromium respectively.

Fail closed: any parse/resolution/classification error raises
:class:`SSRFValidationError` before any network IO.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from blazecrawl_core.network.ip_utils import is_blocked_address, parse_ip_literal

ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}


class SSRFValidationError(ValueError):
    """Raised when a URL violates BlazeCrawl's outbound fetch policy."""


@dataclass(frozen=True)
class ResolvedTarget:
    """Resolved public network target for a URL (legacy shape)."""

    url: str
    hostname: str
    ips: tuple[str, ...]


@dataclass(frozen=True)
class PinnedTarget:
    """A validated target with a single IP the caller MUST connect to."""

    url: str
    scheme: str
    hostname: str
    port: int
    ip: str
    all_ips: tuple[str, ...]


def _blocked_address_message(ip) -> str:
    """Return a user-facing explanation for a blocked address."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    value = str(ip)
    if value == "169.254.169.254":
        return f"URL resolves to cloud metadata endpoint ({value})"
    if ip.is_loopback:
        return f"URL resolves to loopback address ({value})"
    if ip.is_link_local:
        return f"URL resolves to link-local address ({value})"
    if ip.is_private:
        return f"URL resolves to private IP address ({value})"
    if ip.is_unspecified:
        return f"URL resolves to unspecified address ({value})"
    if ip.is_multicast:
        return f"URL resolves to multicast address ({value})"
    return f"URL resolves to reserved or non-public address ({value})"


def _classify_resolved_ip(ip_str: str) -> None:
    """Raise if a *resolved* IP literal falls in a blocked range."""
    parsed = parse_ip_literal(ip_str)
    if parsed is None:
        raise SSRFValidationError(f"Unparseable resolved address: {ip_str!r}")
    if is_blocked_address(parsed):
        raise SSRFValidationError(_blocked_address_message(parsed))


def _parse_and_check_url(url: str, allowed_schemes: tuple[str, ...]) -> tuple[str, str, int]:
    """Validate scheme/host and return ``(scheme, hostname, port)``."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise SSRFValidationError("Only http and https URLs are allowed")

    # Reject userinfo (user:pass@host) — a classic parser-confusion vector.
    if parsed.username is not None or parsed.password is not None:
        raise SSRFValidationError("URLs with embedded credentials are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFValidationError("Invalid URL: no hostname")

    literal = parse_ip_literal(hostname)
    if literal is not None and is_blocked_address(literal):
        raise SSRFValidationError(_blocked_address_message(literal))

    try:
        port = parsed.port if parsed.port is not None else DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise SSRFValidationError("Invalid port") from exc

    return scheme, hostname, port


async def _resolve(hostname: str, port: int) -> list[str]:
    """Resolve a hostname to a de-duplicated, order-preserving list of IPs."""
    try:
        addr_infos = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP),
        )
    except socket.gaierror as exc:
        raise SSRFValidationError("Hostname resolution failed") from exc

    ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        if ip_str not in ips:
            ips.append(ip_str)
    return ips


async def validate_public_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ALLOWED_SCHEMES,
) -> ResolvedTarget:
    """Validate that a URL resolves only to public addresses (legacy API)."""
    pinned = await validate_and_pin(url, allowed_schemes=allowed_schemes)
    return ResolvedTarget(url=pinned.url, hostname=pinned.hostname, ips=pinned.all_ips)


async def validate_and_pin(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ALLOWED_SCHEMES,
) -> PinnedTarget:
    """Resolve once, validate every record, and pin a single IP.

    Raises:
        SSRFValidationError: scheme/host invalid, resolution fails, or ANY
            resolved record is in a blocked range (fail closed).
    """
    scheme, hostname, port = _parse_and_check_url(url, allowed_schemes)
    ips = await _resolve(hostname, port)
    if not ips:
        raise SSRFValidationError("Hostname resolution returned no usable addresses")

    for ip_str in ips:
        _classify_resolved_ip(ip_str)

    pinned_ip = ips[0]

    return PinnedTarget(
        url=url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        ip=pinned_ip,
        all_ips=tuple(ips),
    )


def curl_resolve_entries(
    target: PinnedTarget,
    *,
    extra_ports: tuple[int, ...] = (80, 443),
) -> list[str]:
    """Return libcurl ``--resolve`` entries pinning the host to the validated IP."""
    ports = {target.port, *extra_ports}
    return [f"{target.hostname}:{port}:{target.ip}" for port in sorted(ports)]


def host_resolver_rules(target: PinnedTarget) -> str:
    """Return a Chromium ``--host-resolver-rules`` value pinning host→IP."""
    return f"MAP {target.hostname} {target.ip},MAP {target.hostname}:{target.port} {target.ip}"


__all__ = [
    "SSRFValidationError",
    "ResolvedTarget",
    "PinnedTarget",
    "validate_public_url",
    "validate_and_pin",
    "curl_resolve_entries",
    "host_resolver_rules",
    "ALLOWED_SCHEMES",
]
