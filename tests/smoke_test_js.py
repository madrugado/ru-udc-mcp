#!/usr/bin/env python3
"""End-to-end smoke test for the node MCP server (js/index.js).

Проверяет оба режима данных: локальные шарды (UDC_DATA_DIR) и ленивую загрузку
со статики (по умолчанию https://ru-udc-app.tatnet.app).
Run: uv run python tests/smoke_test_js.py   (нужен node >= 18 и `npm install` в js/)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "js" / "index.js"
JS_DIR = ROOT / "js"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


async def run_session(env_extra: dict[str, str], remote: bool) -> None:
    env = {**os.environ, **env_extra}
    env.pop("UDC_DATA_DIR" if remote else "UDC_DATA_BASE_URL", None)
    params = StdioServerParameters(command=shutil.which("node") or "node", args=[str(SERVER)], cwd=str(JS_DIR), env=env)
    label = "remote-статика" if remote else "локальные шарды"
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            check(f"[{label}] все 5 инструментов объявлены",
                  {"udc_sources", "udc_search", "udc_get", "udc_children", "udc_suggest"} <= tools, str(tools))

            r = await s.call_tool("udc_sources", {})
            data = json.loads(r.content[0].text)
            names = {x["name"] for x in data["sources"]}
            check(f"[{label}] udc_sources видит все три источника", names == {"teacode", "summary", "triumph"}, str(names))
            check(f"[{label}] triumph первый и primary", data["sources"][0]["name"] == "triumph" and data["sources"][0].get("primary") is True)
            check(f"[{label}] источники загрузились", all(x["ok"] for x in data["sources"]), str(data["sources"])[:200])

            r = await s.call_tool("udc_search", {"query": "621.39", "source": "teacode", "limit": 5})
            data = json.loads(r.content[0].text)
            cm = data["results"]["teacode"]["code_matches"]
            check(f"[{label}] поиск по префиксу 621.39", len(cm) >= 5 and all(c["code"].startswith("621.39") for c in cm))

            r = await s.call_tool("udc_search", {"query": "аналитическая химия", "source": "teacode", "limit": 5})
            data = json.loads(r.content[0].text)
            tm = data["results"]["teacode"]["text_matches"]
            check(f"[{label}] поиск 'аналитическая химия' находит 543", any(c["code"] == "543" for c in tm), str(tm[:2]))

            r = await s.call_tool("udc_get", {"code": "004.9", "source": "all"})
            data = json.loads(r.content[0].text)
            srcs = {e["source"] for e in data.get("entries", [])}
            check(f"[{label}] udc_get 004.9 во всех источниках", data.get("ok") is True and srcs == {"teacode", "summary", "triumph"}, str(srcs))
            check(f"[{label}] первая карточка от triumph", data["entries"][0]["source"] == "triumph")

            r = await s.call_tool("udc_get", {"code": "004.9:621.372", "source": "teacode"})
            data = json.loads(r.content[0].text)
            check(f"[{label}] составной код раскладывается", data.get("compound") is True and len(data.get("parts", [])) == 2)

            r = await s.call_tool("udc_children", {"code": "62", "source": "teacode", "limit": 30})
            data = json.loads(r.content[0].text)
            check(f"[{label}] дети 62 непустые", data["sources"]["teacode"]["total"] > 10)

            r = await s.call_tool("udc_search", {"query": "", "limit": 5})
            data = json.loads(r.content[0].text)
            check(f"[{label}] пустой запрос -> ошибка", data.get("ok") is False)

            if not remote:  # suggest по всем источникам тяжёлый — на remote экономим
                ann = (
                    "В статье исследуется применение методов машинного обучения, в частности "
                    "свёрточных нейронных сетей, для автоматической классификации "
                    "медицинских изображений при диагностике онкологических заболеваний."
                )
                r = await s.call_tool("udc_suggest", {"text": ann, "source": "all", "limit": 8})
                data = json.loads(r.content[0].text)
                codes = [t["code"] for t in data.get("top", [])]
                check(f"[{label}] suggest: медицинская тема (61*)", any(c.startswith("61") for c in codes), str(codes[:5]))
                check(f"[{label}] suggest: ИТ-тема (004*)", any(c.startswith("004") for c in codes), str(codes[:5]))
                check(f"[{label}] suggest: у top есть path", all(t.get("path") for t in data.get("top", [])[:3]))


async def main() -> None:
    if shutil.which("node") is None:
        sys.exit("node не найден — нужен Node.js >= 18")
    if not (JS_DIR / "node_modules").is_dir():
        sys.exit("js/node_modules пуст — сначала выполните `npm install` в js/")

    print("== режим 1: локальные шарды (UDC_DATA_DIR) ==")
    await run_session({"UDC_DATA_DIR": str(ROOT / "data")}, remote=False)

    print("== режим 2: ленивая загрузка со статики ==")
    await run_session({}, remote=True)

    print()
    if FAILURES:
        print(f"ИТОГ: {len(FAILURES)} провал(ов): {FAILURES}")
        sys.exit(1)
    print("ИТОГ: все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())
