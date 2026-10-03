#!/usr/bin/env python3
"""End-to-end smoke test: launches server.py over stdio via the MCP client and
exercises every tool. Run: uv run python tests/smoke_test.py"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            for t in ("udc_sources", "udc_search", "udc_get", "udc_children", "udc_suggest"):
                check(f"tool {t} объявлен", t in tools)

            # sources
            r = await s.call_tool("udc_sources", {})
            data = json.loads(r.content[0].text)
            names = {x["name"] for x in data["sources"]}
            check("udc_sources видит все три источника", names == {"teacode", "summary", "triumph"}, str(names))
            first = data["sources"][0]
            check("triumph — основной (первый, primary=true)", first["name"] == "triumph" and first.get("primary") is True, str(first)[:80])
            check("в источниках есть коды", data["total_codes"] > 1000, str(data["total_codes"]))

            # search by code prefix
            r = await s.call_tool("udc_search", {"query": "621.39", "source": "teacode", "limit": 5})
            data = json.loads(r.content[0].text)
            cm = data["results"]["teacode"]["code_matches"]
            check("поиск по префиксу кода 621.39", len(cm) >= 5 and all(c["code"].startswith("621.39") for c in cm))

            # search by words
            r = await s.call_tool("udc_search", {"query": "аналитическая химия", "source": "teacode", "limit": 5})
            data = json.loads(r.content[0].text)
            tm = data["results"]["teacode"]["text_matches"]
            check("поиск 'аналитическая химия' находит 543", any(c["code"] == "543" for c in tm), str(tm[:2]))

            # get with hierarchy
            r = await s.call_tool("udc_get", {"code": "004.9", "source": "all"})
            data = json.loads(r.content[0].text)
            check("udc_get 004.9 ok", data.get("ok") is True)
            srcs = {e["source"] for e in data.get("entries", [])}
            check("004.9 есть во всех источниках", srcs == {"teacode", "summary", "triumph"}, str(srcs))
            check("первая карточка — от triumph (основной)", data["entries"][0]["source"] == "triumph", data["entries"][0]["source"])
            e = next(e for e in data["entries"] if e["source"] == "teacode")
            anc = [a["code"] for a in e["ancestors"]]
            check("предки 004.9 доходят до корня", anc and anc[0] in ("0", "00"), str(anc))
            check("у 004.9 есть дети", len(e.get("children", [])) > 0)

            # compound code decomposition
            r = await s.call_tool("udc_get", {"code": "004.9:621.372", "source": "teacode"})
            data = json.loads(r.content[0].text)
            check("составной код раскладывается", data.get("compound") is True and len(data.get("parts", [])) == 2)

            # children
            r = await s.call_tool("udc_children", {"code": "62", "source": "teacode", "limit": 30})
            data = json.loads(r.content[0].text)
            kids = data["sources"]["teacode"]["children"]
            check("дети 62 непустые", data["sources"]["teacode"]["total"] > 10, str(len(kids)))

            # suggest
            ann = (
                "В статье исследуется применение методов машинного обучения, в частности "
                "свёрточных нейронных сетей, для автоматической классификации "
                "медицинских изображений при диагностике онкологических заболеваний."
            )
            r = await s.call_tool("udc_suggest", {"text": ann, "source": "all", "limit": 8})
            data = json.loads(r.content[0].text)
            check("suggest ok", data.get("ok") is True)
            top = data.get("top", [])
            codes = [t["code"] for t in top]
            check("suggest нашёл медицинскую тематику (61*)", any(c.startswith("61") for c in codes), str(codes[:5]))
            check("suggest нашёл ИТ-тематику (004*)", any(c.startswith("004") for c in codes), str(codes[:5]))
            print(json.dumps(top[:4], ensure_ascii=False, indent=1))

            # error handling
            r = await s.call_tool("udc_search", {"query": "", "source": "bogus"})
            data = json.loads(r.content[0].text)
            check("некорректный источник -> ошибка", data.get("ok") is False and "bogus" in data["error"])

    print()
    if FAILURES:
        print(f"ИТОГ: {len(FAILURES)} провал(ов): {FAILURES}")
        sys.exit(1)
    print("ИТОГ: все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())
