"""BlazeCrawl Core CLI.

Talks to a running BlazeCrawl Core server. Configure with:

    BLAZECRAWL_API_URL   (default http://127.0.0.1:8000)
    BLAZECRAWL_API_KEY   (required unless the server runs loopback auth-disabled)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from blazecrawl_core import __version__


def _client() -> httpx.Client:
    base = os.environ.get("BLAZECRAWL_API_URL", "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("BLAZECRAWL_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(base_url=base, headers=headers, timeout=120.0)


def _print(resp: httpx.Response, as_json: bool, output: Path | None = None) -> int:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text}
        print(json.dumps(detail, indent=2), file=sys.stderr)
        return 1
    data = resp.json()
    if as_json:
        text = json.dumps(data, indent=2)
    else:
        # Human-friendly: prefer markdown.
        md = (data.get("data") or {}).get("markdown") or data.get("markdown")
        text = md or json.dumps(data, indent=2)
    if output is not None:
        try:
            output.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Could not write output to {output}: {exc}", file=sys.stderr)
            return 1
    else:
        print(text)
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    body: dict = {"url": args.url, "render": args.render}
    if args.formats:
        body["formats"] = args.formats
    with _client() as c:
        return _print(c.post("/v1/scrape", json=body), args.json, args.output)


def cmd_map(args: argparse.Namespace) -> int:
    with _client() as c:
        return _print(c.post("/v1/map", json={"url": args.url}), args.json or True)


def cmd_crawl(args: argparse.Namespace) -> int:
    body: dict = {"url": args.url}
    if args.max_pages:
        body["max_pages"] = args.max_pages
    if args.max_depth is not None:
        body["max_depth"] = args.max_depth
    with _client() as c:
        r = c.post("/v1/crawl", json=body)
        if r.status_code >= 400:
            return _print(r, True)
        job = r.json()
        jid = job["job_id"]
        if not args.wait:
            print(json.dumps(job, indent=2))
            return 0
        import time

        for _ in range(600):
            g = c.get(f"/v1/crawl/{jid}")
            st = g.json()
            print(f"status={st.get('status')} crawled={st.get('pages_crawled')}", file=sys.stderr)
            if st.get("status") in ("completed", "failed", "cancelled"):
                print(json.dumps(st, indent=2))
                return 0 if st.get("status") == "completed" else 1
            time.sleep(1)
        print("Timed out waiting for crawl", file=sys.stderr)
        return 1


def cmd_health(args: argparse.Namespace) -> int:
    with _client() as c:
        return _print(c.get("/health"), True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blazecrawl", description="BlazeCrawl Core CLI")
    p.add_argument("--version", action="version", version=f"blazecrawl-core {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="Scrape a URL to Markdown")
    s.add_argument("url")
    s.add_argument("--formats", nargs="+", choices=["markdown", "html", "text", "links", "images"])
    s.add_argument("--render", choices=["auto", "static", "browser"], default="auto")
    s.add_argument("--json", action="store_true")
    s.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Write output to a UTF-8 file instead of stdout (overwrites existing files)",
    )
    s.set_defaults(func=cmd_scrape)

    m = sub.add_parser("map", help="Discover the URL set of a site")
    m.add_argument("url")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_map)

    cr = sub.add_parser("crawl", help="Crawl a site (BFS, same-origin)")
    cr.add_argument("url")
    cr.add_argument("--max-pages", type=int)
    cr.add_argument("--max-depth", type=int)
    cr.add_argument("--wait", action="store_true", help="Block until the crawl completes")
    cr.set_defaults(func=cmd_crawl)

    h = sub.add_parser("health", help="Check server health")
    h.set_defaults(func=cmd_health)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
