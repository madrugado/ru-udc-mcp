#!/usr/bin/env python3
"""Crawl the teacode.com online UDC reference into data/teacode_udc.json.

The site is a set of static generated pages: every node page has a table of
its children (linked codes have their own page, leaf codes are plain rows)
plus notes with "см." cross-references. Navigation is purely relative links,
so the crawler resolves each href against the current page URL.

Usage: python3 scripts/scrape_teacode.py [--workers N] [--max-pages N]
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

BASE = "http://teacode.com/online/udc/"
HOST = "teacode.com"
PATH_PREFIX = "/online/udc/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = DATA_DIR / "teacode_udc.json"
ERR_LOG = DATA_DIR / "teacode_scrape_errors.txt"

ROW_RE = re.compile(r'<tr[^>]*bgcolor="#(eaeaea|c0c0c0)"[^>]*>(.*?)</tr>', re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
A_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
H1_RE = re.compile(r"<h1[^>]*>\s*УДК\s*([^<]+?)\s*</h1>", re.I)
H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S | re.I)


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


def is_udc_page(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and parts.netloc == HOST and parts.path.startswith(PATH_PREFIX)


def parse_page(page_url: str, body: str) -> tuple[dict | None, list[str]]:
    """Return (node_record, [links to enqueue]) and stash row records into RECORDS."""
    links: list[str] = []
    node = None

    m1 = H1_RE.search(body)
    if m1:
        m3 = H3_RE.search(body)
        node = {
            "code": strip_tags(m1.group(1)),
            "title": strip_tags(m3.group(1)) if m3 else "",
            "url": page_url,
        }

    for bg, row in ROW_RE.findall(body):
        cells = TD_RE.findall(row)
        if len(cells) < 3:
            continue
        code_cell, title_cell, notes_cell = cells[0], cells[1], cells[2]
        am = A_RE.search(code_cell)
        code = strip_tags(code_cell)
        if not code:
            continue
        href = am.group(1) if am else None
        if href:
            resolved = urljoin(page_url, href)
            if is_udc_page(resolved):
                links.append(resolved)
        notes = strip_tags(notes_cell)
        if notes.isdigit():  # index pages put "число кодов" into the notes column
            notes = ""
        refs = []
        for rhref, rtext in A_RE.findall(notes_cell):
            if strip_tags(rtext):
                refs.append(strip_tags(rtext))
        RECORDS.append(
            {
                "code": code,
                "title": strip_tags(title_cell),
                "notes": notes,
                "refs": refs,
                "parent_hint": PAGE_CODE[0],
                "linked": bool(href),
                "cancelled": bg == "c0c0c0" or "Исключ" in notes or "Исключ" in strip_tags(title_cell),
            }
        )
    return node, links


RECORDS: list[dict] = []
PAGE_CODE = [""]  # code of the page currently being parsed (set before parse)


def crawl(workers: int, max_pages: int) -> int:
    visited: set[str] = {BASE}
    waves = 0
    errors: list[str] = []
    frontier = [BASE]
    pages_done = 0

    while frontier and pages_done < max_pages:
        batch = frontier[:max_pages - pages_done]
        frontier = frontier[max_pages - pages_done:] if len(frontier) > len(batch) else []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            bodies = list(pool.map(fetch, batch))
        next_links: list[str] = []
        for url, body in zip(batch, bodies):
            pages_done += 1
            if body is None:
                errors.append(f"FETCH FAIL {url}")
                continue
            PAGE_CODE[0] = url.rstrip("/").rsplit("/", 1)[-1][:-5] if url.endswith(".html") else ""
            try:
                node, links = parse_page(url, body)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"PARSE FAIL {url}: {exc}")
                continue
            if node:
                NODES[node["code"]] = node
            for link in links:
                if link not in visited:
                    visited.add(link)
                    next_links.append(link)
        frontier.extend(next_links)
        waves += 1
        print(
            f"wave {waves}: +{len(batch)} pages (total {pages_done}), "
            f"queue {len(frontier)}, rows {len(RECORDS)}, nodes {len(NODES)}, errors {len(errors)}",
            flush=True,
        )
    if errors:
        ERR_LOG.write_text("\n".join(errors), encoding="utf-8")
    print(f"done: {pages_done} pages, {len(RECORDS)} rows, {len(NODES)} node pages, {len(errors)} errors")
    return pages_done


NODES: dict[str, dict] = {}


def main() -> None:
    global NODES, RECORDS
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=200_000)
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NODES, RECORDS = {}, []

    t0 = time.time()
    pages_done = crawl(args.workers, args.max_pages)

    # Deduplicate rows: a code may be listed on several pages (cross-references).
    merged: dict[str, dict] = {}
    for rec in RECORDS:
        code = rec["code"]
        prev = merged.get(code)
        if prev is None:
            merged[code] = rec
        else:
            # prefer a hint that is the code's longest proper prefix (true UDC parent)
            if (
                code.startswith(rec["parent_hint"])
                and len(rec["parent_hint"]) > len(prev["parent_hint"])
                and rec["parent_hint"] != code
            ):
                rec = {**prev, "parent_hint": rec["parent_hint"]}
                merged[code] = rec
            if rec["title"] and not prev["title"]:
                merged[code]["title"] = rec["title"]
            if rec["notes"] and not prev["notes"]:
                merged[code]["notes"] = rec["notes"]
            if rec["linked"]:
                merged[code]["linked"] = True

    records = sorted(merged.values(), key=lambda r: r["code"])
    meta = {
        "source": "teacode",
        "base_url": "http://teacode.com/online/udc/",
        "retrieved": time.strftime("%Y-%m-%d"),
        "language": "ru",
        "edition_note": (
            "Электронный справочник teacode.com (снят с сайта; сайт не обновлялся после ~2015 г., "
            "основа — русскоязычные таблицы УДК). Часть кодов помечена как исключённые."
        ),
        "pages_crawled": pages_done,
        "records": len(records),
    }
    OUT_FILE.write_text(
        json.dumps({"meta": meta, "records": records}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"saved {len(records)} unique codes -> {OUT_FILE} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
