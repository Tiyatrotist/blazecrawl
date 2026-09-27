"""Crawler for BlazeCrawl Core (BFS, same-origin, robots-respecting).

Job state is held in-process by default (``CRAWL_QUEUE_BACKEND=memory``) which
is sufficient for a single-node self-host. A Redis-backed queue can be enabled
for multi-process worker fleets; the job contract is identical either way.

Security invariants preserved on every page fetch:
* every URL is SSRF-validated and pinned before fetching
* robots.txt is enforced (cannot be disabled in OSS core)
* only same-origin http/https URLs are followed
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlsplit

from blazecrawl_core.config import settings
from blazecrawl_core.engine import robots
from blazecrawl_core.engine.scraper import scrape
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.ssrf import SSRFValidationError, validate_and_pin

logger = get_logger(__name__)


@dataclass
class CrawlJobState:
    job_id: str
    seed: str
    status: str = "queued"  # queued|running|completed|failed|cancelled
    max_pages: int = settings.CRAWL_MAX_PAGES_DEFAULT
    max_depth: int = settings.CRAWL_MAX_DEPTH_DEFAULT
    user_agent: str = robots.DEFAULT_USER_AGENT
    pages: list[dict] = field(default_factory=list)
    pages_crawled: int = 0
    pages_failed: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "seed": self.seed,
            "status": self.status,
            "pages_crawled": self.pages_crawled,
            "pages_failed": self.pages_failed,
            "max_pages": self.max_pages,
            "user_agent": self.user_agent,
            "error": self.error,
            "pages": self.pages,
        }


class CrawlManager:
    """In-process crawl job manager + executor (single-node)."""

    def __init__(self) -> None:
        self._jobs: dict[str, CrawlJobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(settings.CRAWL_MAX_CONCURRENCY)

    def create_job(
        self,
        seed: str,
        max_pages: int | None,
        max_depth: int | None,
        user_agent: str = robots.DEFAULT_USER_AGENT,
    ) -> CrawlJobState:
        job = CrawlJobState(
            job_id=str(uuid.uuid4()),
            seed=seed,
            max_pages=max_pages or settings.CRAWL_MAX_PAGES_DEFAULT,
            max_depth=max_depth or settings.CRAWL_MAX_DEPTH_DEFAULT,
            user_agent=user_agent,
        )
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    def get_job(self, job_id: str) -> CrawlJobState | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if not job or not task:
            return False
        task.cancel()
        job.status = "cancelled"
        job.finished_at = time.time()
        return True

    async def _run(self, job: CrawlJobState) -> None:
        job.status = "running"
        try:
            await self._crawl(job)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error("Crawl failed", job_id=job.job_id, error=str(e))
        finally:
            job.finished_at = time.time()

    async def _crawl(self, job: CrawlJobState) -> None:
        seed_host = urlsplit(job.seed).netloc
        visited: set[str] = set()
        frontier: list[tuple[str, int]] = [(job.seed, 0)]

        while frontier and job.pages_crawled < job.max_pages:
            url, depth = frontier.pop(0)
            url = urldefrag(url)[0]
            if url in visited or depth > job.max_depth:
                continue
            visited.add(url)

            # SSRF validation (defense-in-depth; seed validated at enqueue).
            try:
                await validate_and_pin(url)
            except SSRFValidationError:
                logger.warning("Crawl skipping SSRF-blocked URL", url=url)
                continue

            # robots.txt
            if settings.RESPECT_ROBOTS_TXT and not await robots.is_allowed(
                url, user_agent=job.user_agent
            ):
                logger.info("Crawl skipping robots-disallowed URL", url=url)
                continue

            async with self._sem:
                try:
                    result = await scrape(
                        url,
                        formats=["markdown", "links"],
                        render="auto",
                        user_agent=job.user_agent,
                    )
                    job.pages_crawled += 1
                    job.pages.append(
                        {
                            "url": result.final_url,
                            "status_code": result.status_code,
                            "title": result.metadata.get("title"),
                            "markdown": result.markdown,
                        }
                    )
                    if depth < job.max_depth:
                        for link in result.links or []:
                            lu = link.get("url")
                            if not lu:
                                continue
                            lu = urldefrag(lu)[0]
                            if lu in visited:
                                continue
                            if urlsplit(lu).netloc != seed_host:
                                continue
                            if urlsplit(lu).scheme not in ("http", "https"):
                                continue
                            frontier.append((lu, depth + 1))
                except SSRFValidationError:
                    continue
                except Exception as e:
                    job.pages_failed += 1
                    logger.warning("Crawl page failed", url=url, error=str(e))


_manager: CrawlManager | None = None


def get_crawl_manager() -> CrawlManager:
    global _manager
    if _manager is None:
        _manager = CrawlManager()
    return _manager
