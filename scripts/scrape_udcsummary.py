#!/usr/bin/env python3
"""Scrape the official UDC Summary (udcsummary.info, Russian) into data/udcsummary_ru.json.

The site renders each top-level section as a page whose dtree script contains
the full subtree of that section as d.add(nid, pid, tag, htmlLabel, urlOrCode,
title, ...) calls. The root index lists the sections (auxiliary + main tables).

UDC Summary is © UDC Consortium, released under CC BY-NC-ND (attribution kept
in the JSON metadata).
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from pathlib import Path

BASE = "http://udcsummary.info/php/index.php?lang=ru"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = DATA_DIR / "udcsummary_ru.json"

DADD_CALLS: list[list[str]] = []


def parse_dadd(text: str) -> list[list[str]]:
    """Tokenize every d.add(...) call into top-level comma-separated args."""
    calls: list[list[str]] = []
    idx = 0
    while True:
        i = text.find("d.add(", idx)
        if i < 0:
            break
        j = i + len("d.add(")
        args: list[str] = []
        cur = ""
        in_str = False
        esc = False
        depth = 0
        just_closed = False  # a quoted string was just appended; next comma must not add an empty token
        ok = True
        while True:
            if j >= len(text):
                ok = False
                break
            ch = text[j]
            if in_str:
                if esc:
                    cur += ch
                    esc = False
                elif ch == "\\":
                    cur += ch
                    esc = True
                elif ch == "'":
                    in_str = False
                    args.append(cur)
                    cur = ""
                    just_closed = True
                else:
                    cur += ch
            else:
                if ch == "(":
                    depth += 1
                    cur += ch
                    just_closed = False
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                    cur += ch
                elif ch == "," and depth == 0:
                    if not just_closed:
                        args.append(cur.strip())
                    cur = ""
                    just_closed = False
                elif ch == "'":
                    in_str = True
                    cur = ""
                    just_closed = False
                else:
                    cur += ch
                    just_closed = False
            j += 1
        if not ok:
            break
        idx = j + 1
        if cur.strip() and not just_closed:
            args.append(cur.strip())  # final bare token before ')'
        calls.append(args)
    return calls


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).replace("\xa0", " ").strip()


def label_title(label: str) -> str:
    """Drop the leading code badge (nodetag div/span) and strip HTML."""
    label = re.sub(r'<div class="nodetag"[^>]*>.*?</div>', "", label, flags=re.S)
    label = re.sub(r'<span class="nodetag"[^>]*>.*?</span>', "", label, flags=re.S)
    return strip_tags(label)


def fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "RuUDC-MCP-crawler/1.0 (one-time dataset snapshot)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def nodes_from_page(body: str) -> list[dict]:
    """Parse one page's d.add tree into records with local parent pointers."""
    out = []
    for args in parse_dadd(body):
        if len(args) < 6 or not args[0].lstrip("-").isdigit():
            continue
        nid, pid = args[0], args[1]
        tag, label, target, title = args[2], args[3], args[4], args[5]
        notes = next((a for a in args[6:9] if a), "")
        if not title:
            title = label_title(label)
        out.append(
            {
                "nid": int(nid),
                "pid": int(pid),
                "code": tag.strip(),
                "title": re.sub(r"\s+", " ", title).strip(),
                "note": notes.strip(),
                "has_page": target.startswith("index.php?id="),
            }
        )
    return out


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    root_body = fetch(BASE)
    root_nodes = nodes_from_page(root_body)
    sec_urls = sorted(set(re.findall(r"index\.php\?id=\d+&lang=ru", root_body)))
    print(f"root page: {len(root_nodes)} nodes, {len(sec_urls)} sections to fetch")

    records: list[dict] = []

    def add(rec: dict) -> None:
        records.append(
            {
                "code": rec["code"],
                "title": rec["title"],
                "note": rec["note"],
                "parent": rec["_parent"],
                "is_group": rec["_is_group"],
            }
        )

    # root-level structure: flatten the two group headers into the virtual root
    by_nid = {n["nid"]: n for n in root_nodes}
    children: dict[int, list[int]] = {}
    for n in root_nodes:
        children.setdefault(n["pid"], []).append(n["nid"])
    for nid in children.get(0, []):
        n = by_nid[nid]
        if n["has_page"]:
            continue  # sections handled below
        for cid in children.get(nid, []):  # group header -> its children are sections
            sec = by_nid[cid]
            add({**sec, "_parent": "", "_is_group": False})

    fetched = 0
    for suffix in sec_urls:
        url = f"http://udcsummary.info/php/{suffix}"
        nodes = nodes_from_page(fetch(url))
        fetched += 1
        by = {n["nid"]: n for n in nodes}
        kids: dict[int, list[int]] = {}
        for n in nodes:
            kids.setdefault(n["pid"], []).append(n["nid"])
        for n in nodes:
            if n["pid"] == -1:
                parent = ""  # section root attaches to the virtual root
            else:
                p = by.get(n["pid"])
                parent = p["code"] if p else ""
            add({**n, "_parent": parent, "_is_group": set(n["code"]) <= {"-"}})
        print(f"  {url}: {len(nodes)} nodes")
        time.sleep(0.3)

    # dedupe by (code, parent)
    seen: set[tuple[str, str]] = set()
    unique = []
    for r in records:
        key = (r["code"], r["parent"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    meta = {
        "source": "summary",
        "base_url": "http://udcsummary.info/php/index.php?lang=ru",
        "retrieved": time.strftime("%Y-%m-%d"),
        "language": "ru",
        "license": "UDC Summary © UDC Consortium, CC BY-NC-ND 3.0",
        "edition_note": (
            "Официальный UDC Summary (сокращённые основные таблицы УДК, ~2-3 уровня) "
            "в русском переводе; поддерживается UDC Consortium."
        ),
        "sections_fetched": fetched,
        "records": len(unique),
    }
    OUT_FILE.write_text(
        json.dumps({"meta": meta, "records": unique}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"saved {len(unique)} codes -> {OUT_FILE} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
