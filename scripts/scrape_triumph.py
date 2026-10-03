#!/usr/bin/env python3
"""Crawl the Triumph online UDC classifier (2026 edition) into data/triumph_udc.json.

The site is a dynamic tree: each node page udk.html?category_id=N has a table of
children (code + title), child rows link to deeper pages by category_id; ranges
like "21/29" are nodes whose pages list their members. The node's own code is
in the page <title> as "УДК <code> | <name>". Footer rows are Cyrillic-only and
are filtered out by the code charset.

Usage: python3 scripts/scrape_triumph.py [--workers N] [--max-pages N]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE = "https://www.triumph.ru/html/serv/udk.html"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = DATA_DIR / "triumph_udc.json"
ERR_LOG = DATA_DIR / "triumph_scrape_errors.txt"

TITLE_RE = re.compile(r"<title>УДК\s+([^<]+?)</title>", re.I)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
CAT_RE = re.compile(r"category_id=(\d+)")
# codes: digits/latin start, then UDC aux chars; excludes Cyrillic footer rows
CODE_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z+\-/:=()."\[\]`\' ]{0,24}$')

RECORDS: list[dict] = []
PAGE_CODE = [""]


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).replace("\xa0", " ").strip()


def fetch(url: str, timeout: float = 25.0, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "RuUDC-MCP-crawler/1.0 (one-time dataset snapshot)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == retries:
                return None
            time.sleep(1.0 + attempt)
    return None


def page_code(body: str) -> str:
    """Own code of a tree page: first token of '<title>УДК <code> <name>'."""
    m = TITLE_RE.search(body)
    if not m:
        return ""
    token = m.group(1).strip().split(" ")[0].rstrip(".")
    return token if CODE_RE.match(token) else ""


def parse_page(page_url: str, body: str) -> list[str]:
    """Parse one tree page: rows -> RECORDS, links -> return value."""
    PAGE_CODE[0] = page_code(body)

    links: list[str] = []
    for row in ROW_RE.findall(body):
        cells = [strip_tags(c) for c in TD_RE.findall(row)]
        if len(cells) < 2:
            continue
        cat = CAT_RE.search(row)
        code = cells[0]
        title = cells[-1]
        if not code or not CODE_RE.match(code):
            continue
        if cat:
            links.append(f"{BASE}?category_id={cat.group(1)}")
        RECORDS.append(
            {
                "code": code.strip(),
                "title": title,
                "notes": "",
                "refs": [],
                "parent_hint": PAGE_CODE[0],
                "linked": bool(cat),
                "cancelled": False,
            }
        )
    return links


NODES: dict[str, dict] = {}


def crawl(workers: int, max_pages: int) -> int:
    visited: set[str] = {BASE}
    frontier = [BASE]
    pages_done = 0
    errors: list[str] = []
    waves = 0

    while frontier and pages_done < max_pages:
        batch = frontier[: max_pages - pages_done]
        frontier = frontier[len(batch):]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            bodies = list(pool.map(fetch, batch))
        next_links: list[str] = []
        for url, body in zip(batch, bodies):
            pages_done += 1
            if body is None:
                errors.append(f"FETCH FAIL {url}")
                continue
            try:
                links = parse_page(url, body)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"PARSE FAIL {url}: {exc}")
                continue
            if PAGE_CODE[0]:
                NODES[PAGE_CODE[0]] = {"url": url}
            for link in links:
                if link not in visited:
                    visited.add(link)
                    next_links.append(link)
        frontier.extend(next_links)
        waves += 1
        print(
            f"wave {waves}: +{len(batch)} pages (total {pages_done}), queue {len(frontier)}, "
            f"rows {len(RECORDS)}, errors {len(errors)}",
            flush=True,
        )
    if errors:
        ERR_LOG.write_text("\n".join(errors), encoding="utf-8")
    print(f"done: {pages_done} pages, {len(RECORDS)} rows, {len(errors)} errors")
    return pages_done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-pages", type=int, default=150_000)
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    pages_done = crawl(args.workers, args.max_pages)

    merged: dict[str, dict] = {}
    for rec in RECORDS:
        code = rec["code"]
        prev = merged.get(code)
        if prev is None:
            merged[code] = rec
        else:
            if (
                code.startswith(rec["parent_hint"])
                and len(rec["parent_hint"]) > len(prev["parent_hint"])
                and rec["parent_hint"] != code
            ):
                merged[code] = {**prev, "parent_hint": rec["parent_hint"]}
            if rec["title"] and not merged[code]["title"]:
                merged[code]["title"] = rec["title"]

    records = sorted(merged.values(), key=lambda r: (r["code"],))
    # compact form: drop empty optional keys, no pretty-print (dataset is large)
    for r in records:
        for k in ("notes", "refs", "parent_hint"):
            if not r.get(k):
                r.pop(k, None)
        if not r.get("linked"):
            r.pop("linked", None)
        if not r.get("cancelled"):
            r.pop("cancelled", None)
    meta = {
        "source": "triumph",
        "base_url": "https://www.triumph.ru/html/serv/udk.html",
        "retrieved": time.strftime("%Y-%m-%d"),
        "language": "ru",
        "edition_note": (
            "«УДК Классификатор 2026» издательства Триумф — современная русская редакция "
            "таблиц УДК с глубиной; снят обходом дерева category_id."
        ),
        "pages_crawled": pages_done,
        "records": len(records),
    }
    OUT_FILE.write_text(
        json.dumps({"meta": meta, "records": records}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"saved {len(records)} unique codes -> {OUT_FILE} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
