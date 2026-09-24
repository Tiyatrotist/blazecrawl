"""robots.txt handling for BlazeCrawl Core (OSS).

Fetches and evaluates robots.txt through the SSRF-safe egress layer. Fails
*safe*: when robots cannot be fetched, crawlers must assume "allowed but be
polite" for well-known public content while still never bypassing an explicit
disallow that WAS retrieved. There is no configuration to disable robots
enforcement in the OSS core.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.egress import safe_fetch
from blazecrawl_core.network.ssrf import SSRFValidationError

logger = get_logger(__name__)

__all__ = ["clear_cache", "get_crawl_delay", "is_allowed"]

_DEFAULT_UA = "*"


@dataclass(frozen=True)
class _RobotsParser:
    parser: RobotFileParser
    rules: tuple[tuple[tuple[str, ...], tuple[tuple[str, bool], ...]], ...]


_cache: dict[str, tuple[float, _RobotsParser | None]] = {}
_CACHE_TTL = 900.0  # 15 minutes


def _robots_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def clear_cache() -> None:
    """Clear the in-memory robots.txt cache."""
    _cache.clear()


async def _get_parser(url: str) -> _RobotsParser | None:
    origin_key = urlsplit(url).netloc
    now = time.time()

    cached = _cache.get(origin_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    rp = None
    try:
        resp = await safe_fetch(_robots_url(url), timeout_s=10.0, max_bytes=512 * 1024)
        if resp.status_code == 200:
            rp = RobotFileParser()
            lines = resp.text.splitlines()
            rp.parse(lines)
            rules = []
            agents = []
            group_rules = []
            for line in lines + ["User-agent:"]:
                directive, _, value = line.partition(":")
                directive = directive.strip().lower()
                value = value.split("#", 1)[0].strip()
                if directive == "user-agent":
                    if group_rules:
                        rules.append((tuple(agents), tuple(group_rules)))
                        agents = []
                        group_rules = []
                    if value:
                        agents.append(value.lower())
                elif directive in {"allow", "disallow"} and agents:
                    if value:
                        group_rules.append((value, directive == "allow"))
            rp = _RobotsParser(rp, tuple(rules))
    except SSRFValidationError:
        raise
    except Exception as e:
        logger.debug("robots.txt fetch failed; treating as allowed", error=str(e))
        rp = None

    _cache[origin_key] = (now, rp)
    return rp


async def is_allowed(url: str, user_agent: str = _DEFAULT_UA) -> bool:
    """Return True when robots.txt permits fetching ``url``.

    Fetches/parses robots.txt (cached per-origin). On fetch/parse failure we
    return True (standard crawler behaviour for unreachable robots), but we
    never *fetch* a URL whose retrieved robots.txt disallows it.
    """
    robots = await _get_parser(url)
    if robots is None:
        return True
    try:
        parsed_url = urlsplit(unquote(url))
        target = quote(
            urlunsplit(("", "", parsed_url.path, parsed_url.query, parsed_url.fragment)) or "/"
        )
        agent = user_agent.split("/", 1)[0].lower()
        groups = [
            group
            for group in robots.rules
            if any(name == "*" or name in agent for name in group[0])
        ]
        if not groups:
            return True
        selected = next(
            (group for group in groups if any(name != "*" for name in group[0])),
            groups[0],
        )
        matching_rules = [
            rule
            for rule in selected[1]
            if target.startswith(quote(rule[0])) or _robots_pattern_matches(rule[0], target)
        ]
        if not matching_rules:
            return True
        longest = max(len(rule[0]) for rule in matching_rules)
        return next(rule[1] for rule in matching_rules if len(rule[0]) == longest)
    except Exception:
        return True


def _robots_pattern_matches(pattern: str, target: str) -> bool:
    if "*" not in pattern and "$" not in pattern:
        return False
    regex = re.escape(pattern).replace(r"\*", ".*")
    if pattern.endswith("$"):
        regex = regex[:-2] + "$"
    return re.fullmatch(regex, target) is not None


async def get_crawl_delay(url: str, user_agent: str = _DEFAULT_UA) -> float | None:
    """Return the crawl-delay in seconds for user_agent if specified, else None."""
    robots = await _get_parser(url)
    if robots is None:
        return None
    try:
        delay = robots.parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None
    except Exception:
        return None
