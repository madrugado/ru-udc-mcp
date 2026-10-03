#!/usr/bin/env python3
"""RuUDC — MCP-сервер для УДК (универсальной десятичной классификации).

Источники (данные лежат в data/*.json, снимки делаются скриптами в scripts/):
  * teacode   — электронный справочник teacode.com/online/udc (126k+ кодов, рус.);
  * summary   — официальный UDC Summary (UDC Consortium, CC BY-NC-ND, рус.).

Инструменты:
  udc_sources  — список источников и их состояние;
  udc_search   — поиск по коду/названию;
  udc_get      — карточка кода: описание, примечания, предки, дети;
  udc_children — прямой вывод дочерних кодов;
  udc_suggest  — подбор кодов УДК по свободному тексту (аннотации статьи).

Запуск: uv run server.py   (транспорт stdio)
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DATA_DIR = Path(os.environ.get("UDC_DATA_DIR") or Path(__file__).resolve().parent / "data")
MAX_CHILDREN = 500

mcp = MCPServer(
    "ruudc",
    instructions=(
        "Сервер УДК (Universal Decimal Classification). Ищите коды через udc_search / "
        "udc_suggest, смотрите иерархию через udc_get и udc_children. Источник 'teacode' — "
        "детальные русские таблицы (может содержать устаревшие/исключённые коды), 'summary' — "
        "официальный краткий свод UDC Consortium. УДК-композиции (уточнение темы) строятся "
        "знаками ':' (отношение), '+' (объединение), '/' (диапазон) из простых кодов."
    ),
)

# ---------------------------------------------------------------- text utils

RU_STOP = set(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по ее мне "
    "было вот от меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него "
    "до вас ведь там потом себя ничего ей может они тут где есть надо ней для мы их чем была "
    "сам без чего раз тоже себе под будет кто этот того потому этого какой совсем ним здесь "
    "этом один почти мой тем чтобы нее куда зачем всех можно при два об другой хоть после над "
    "больше тот через эти нас про всего них эта много три эту моя своей этой перед иногда том "
    "такой им более всегда между поэтому также также т.е т.д т.п т.п. т.д.".split()
)
EN_STOP = set(
    "the a an of and or to in on for with as by at is are be this that it its from into about "
    "not no nor but if then than so such can could may might will would should have has had "
    "was were been being do does did done their there these those which who whom what when "
    "where why how all any both each few more most other some only own same too very".split()
)
STOP = RU_STOP | EN_STOP


# латинские гомоглифы, которые часто вкраплены в кириллицу (например "Mедицина")
_HOMOGLYPHS = str.maketrans("aeyopckmx", "аеуорскмх")


def norm(s: str) -> str:
    s = (s or "").casefold().replace("ё", "е")
    s = s.translate(_HOMOGLYPHS)
    s = re.sub(r"[^\w\s:./+\-()]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


_STEM_1 = (
    "иями", "ями", "ами", "иями", "иях", "ях", "ах", "ов", "ев", "ой", "ый", "ий",
    "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ому", "ему", "ого", "его",
    "ую", "ью", "ия", "ии", "ию", "ом", "ем", "ам", "ям", "ей", "а", "я", "ы",
    "и", "о", "е", "й", "ь", "у", "ю",
)
_STEM_2 = ("ическ", "ическ", "ческ", "енн", "нн", "изм", "ость", "ств", "ций", "тель")


def stem(w: str) -> str:
    """Лёгкий русский стеммер (без словаря): сливает падежные формы."""
    for suf in _STEM_1:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    if w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) >= 5:
        w = w[:-1]
    for suf in _STEM_2:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w


def tokens(s: str) -> list[str]:
    out = []
    for t in re.split(r"[\s/:+()\[\];,.]+", norm(s)):
        t = t.strip("-")
        if not t or t in STOP:
            continue
        if len(t) >= 3 or (len(t) == 2 and not t.isdigit()):
            out.append(t)
    return out


# ---------------------------------------------------------------- data model


class Source:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = path
        self.meta: dict = {}
        self.recs: list[dict] = []
        self.by_code: dict[str, list[dict]] = {}
        self.children: dict[str, list[str]] = {}
        self.norm_texts: list[str] = []
        self.stem_texts: list[str] = []
        self.title_tokens: list[frozenset[str]] = []
        self.idf: dict[str, float] = {}
        self.loaded = False
        self.error = ""

    def load(self) -> None:
        if self.loaded or self.error:
            return
        if not self.path.exists():
            self.error = f"файл данных отсутствует: {self.path.name} (запустите scripts/scrape_*.py)"
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
            self.meta = blob.get("meta", {})
            self.recs = blob.get("records", [])
        except Exception as exc:  # noqa: BLE001
            self.error = f"не удалось прочитать {self.path.name}: {exc}"
            return
        df: Counter = Counter()
        for i, r in enumerate(self.recs):
            self.by_code.setdefault(r["code"], []).append(r)
            parent = r.get("parent") or r.get("parent_hint") or ""
            self.children.setdefault(parent, []).append(r["code"])
            title = r.get("title") or ""
            notes = r.get("note") or r.get("notes") or ""
            self.norm_texts.append(norm(title) + " | " + norm(notes))
            toks = frozenset(stem(t) for t in tokens(title))
            self.title_tokens.append(toks)
            self.stem_texts.append(" ".join(stem(t) for t in tokens(title + " " + notes)))
            for t in toks:
                df[t] += 1
        n = max(len(self.recs), 1)
        self.idf = {t: math.log(1.0 + n / c) for t, c in df.items()}
        self.loaded = True

    def rec_title(self, r: dict) -> str:
        return r.get("title") or ""

    def entry(self, r: dict, with_children: bool = True) -> dict:
        parent = r.get("parent") or r.get("parent_hint") or ""
        e = {
            "code": r["code"],
            "title": self.rec_title(r),
            "source": self.name,
        }
        if r.get("note") or r.get("notes"):
            e["note"] = r.get("note") or r.get("notes")
        if r.get("refs"):
            e["cross_references"] = r["refs"]
        if r.get("cancelled"):
            e["cancelled"] = True
        if self.name == "teacode" and r.get("linked"):
            e["url"] = teacode_url(r["code"])
        e["ancestors"] = self.ancestors(r["code"])
        if parent:
            e["parent"] = parent
        if with_children:
            kids = self.children.get(r["code"], [])
            e["children"] = [
                {"code": c, "title": self.rec_title(self.by_code[c][0])} for c in kids[:MAX_CHILDREN]
            ]
            if len(kids) > MAX_CHILDREN:
                e["children_truncated"] = len(kids) - MAX_CHILDREN
        return e

    def ancestors(self, code: str) -> list[dict]:
        chain: list[dict] = []
        seen = {code}
        cur = self.first(code)
        while cur is not None and len(chain) < 30:
            parent = cur.get("parent") or cur.get("parent_hint") or ""
            if not parent or parent in seen:
                break
            p = self.first(parent)
            if p is None:
                chain.append({"code": parent, "title": ""})
                break
            chain.append({"code": parent, "title": self.rec_title(p)})
            seen.add(parent)
            cur = p
        chain.reverse()
        return chain

    def first(self, code: str) -> dict | None:
        lst = self.by_code.get(code)
        return lst[0] if lst else None

    def has_children(self, code: str) -> bool:
        return bool(self.children.get(code))


def teacode_url(code: str) -> str | None:
    c = code.strip()
    if not re.match(r"^\d", c):
        return None
    if c.startswith("00"):
        sec = "00"
    elif c[0] in "12789":
        sec = c[0]
    else:
        sec = c[:2]
    return f"http://teacode.com/online/udc/{sec}/{c}.html"


SOURCES: dict[str, Source] = {
    "teacode": Source("teacode", DATA_DIR / "teacode_udc.json"),
    "summary": Source("summary", DATA_DIR / "udcsummary_ru.json"),
}


def resolve_sources(source: str) -> tuple[list[Source] | None, str | None]:
    if source == "all":
        srcs = list(SOURCES.values())
    elif source in SOURCES:
        srcs = [SOURCES[source]]
    else:
        return None, f"неизвестный источник: {source!r} (доступны: all, teacode, summary)"
    for s in srcs:
        s.load()
        if s.error:
            return None, f"источник {s.name}: {s.error}"
    return srcs, None


# ---------------------------------------------------------------- scoring


def _match_score(src: Source, i: int, q_tokens: list[str], phrase: str, idf_sum: float) -> float:
    text = src.norm_texts[i]
    stems = src.stem_texts[i]
    ttoks = src.title_tokens[i]
    score = 0.0
    for t in q_tokens:
        st = stem(t)
        if st in ttoks:
            score += src.idf.get(st, 1.0) * 1.3
        elif st in stems:
            score += src.idf.get(st, 1.0)
        elif t in text:
            score += src.idf.get(st, 1.0) * 0.7
    if idf_sum > 0:
        score /= idf_sum
    if phrase and len(phrase) >= 4 and phrase in text:
        score += 0.5
    return score


def _search_in_source(src: Source, query: str, limit: int) -> tuple[list[dict], list[dict]]:
    """Return (code_matches, text_matches) ranked."""
    code_matches: list[dict] = []
    q_norm = norm(query).replace(" ", "")
    if q_norm:
        for code in src.by_code:
            if code == q_norm or code.startswith(q_norm):
                code_matches.append(code)
        code_matches.sort(key=lambda c: (len(c), c))
        code_matches = code_matches[:limit]

    q_tokens = tokens(query)
    phrase = norm(query)
    idf_sum = sum(src.idf.get(t, 1.0) for t in q_tokens)
    text_matches: list[dict] = []
    if q_tokens:
        scored = []
        for i, r in enumerate(src.recs):
            sc = _match_score(src, i, q_tokens, phrase, idf_sum)
            if sc > 0:
                scored.append((sc, r["code"], i))
        scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        for sc, code, i in scored[:limit]:
            r = src.recs[i]
            text_matches.append(
                {
                    "code": code,
                    "title": src.rec_title(r),
                    "source": src.name,
                    "score": round(sc, 3),
                    "has_children": src.has_children(code),
                    **({"cancelled": True} if r.get("cancelled") else {}),
                }
            )
    return code_matches, text_matches


def _code_match_entries(codes: list[str], src: Source, limit: int) -> list[dict]:
    out = []
    for c in codes[:limit]:
        r = src.first(c)
        out.append(
            {
                "code": c,
                "title": src.rec_title(r) if r else "",
                "source": src.name,
                "has_children": src.has_children(c),
                **({"cancelled": True} if r and r.get("cancelled") else {}),
            }
        )
    return out


COMBINE_SPLIT_RE = re.compile(r"[+:\[\]]")


# ---------------------------------------------------------------- tools


@mcp.tool()
def udc_sources() -> dict:
    """Список доступных источников УДК: объём, происхождение, дата обновления.

    Returns dict with 'sources' list and 'total_codes'.
    """
    out = []
    for s in SOURCES.values():
        s.load()
        info = {
            "name": s.name,
            "codes": len(s.recs),
            "ok": s.loaded,
        }
        if s.error:
            info["error"] = s.error
        info.update(
            {
                "base_url": s.meta.get("base_url"),
                "retrieved": s.meta.get("retrieved"),
                "language": s.meta.get("language"),
                "license": s.meta.get("license"),
                "edition_note": s.meta.get("edition_note"),
            }
        )
        out.append(info)
    return {"sources": out, "total_codes": sum(i["codes"] for i in out)}


@mcp.tool()
def udc_search(query: str, source: str = "all", limit: int = 15) -> dict:
    """Поиск кодов УДК по коду (префикс) и/или по названию (русские или английские слова).

    query: строка запроса, например 'нейронные сети', 'машинное обучение', '621.37', 'gis'.
    source: 'all' | 'teacode' | 'summary'. limit: максимум результатов на источник.
    Возвращает code_matches (по коду) и text_matches (по названиям, с релевантностью).
    """
    srcs, err = resolve_sources(source)
    if err:
        return {"ok": False, "error": err}
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "пустой запрос"}
    limit = max(1, min(int(limit), 50))
    results = {}
    total = 0
    for s in srcs:
        code_m, text_m = _search_in_source(s, query, limit)
        results[s.name] = {
            "code_matches": _code_match_entries(code_m, s, limit),
            "text_matches": text_m,
        }
        total += len(code_m) + len(text_m)
    if total == 0:
        results["hint"] = (
            "Ничего не найдено. Попробуйте более общие термины, другую форму слова "
            "(УДК-заголовки часто в единственном числе: 'нейронная сеть'), или источник по отдельности."
        )
    return {"ok": True, "query": query, "results": results}


@mcp.tool()
def udc_get(code: str, source: str = "all") -> dict:
    """Полная карточка кода УДК: описание, примечания, цепочка предков, прямые дети.

    code: код УДК, например '621.372', '004.9', '616-006'. Составные коды ('621.372:004.9',
    '62+68') автоматически раскладываются на составляющие с объяснением.
    """
    srcs, err = resolve_sources(source)
    if err:
        return {"ok": False, "error": err}
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "пустой код"}

    entries = []
    for s in srcs:
        r = s.first(code)
        if r:
            entries.append(s.entry(r))

    if not entries:
        parts = [p.strip() for p in COMBINE_SPLIT_RE.split(code) if p.strip()]
        if len(parts) > 1:
            resolved = []
            for p in parts:
                found = []
                for s in srcs:
                    pr = s.first(p)
                    if pr:
                        found.append({"code": p, "title": s.rec_title(pr), "source": s.name})
                resolved.append({"part": p, "matches": found})
            return {
                "ok": True,
                "code": code,
                "compound": True,
                "note": (
                    "Это составной код УДК (знаки ':' — отношение тем, '+' — объединение, "
                    "':' повторяется между частями). Ниже — найденные составляющие; "
                    "уточните каждую через udc_get при необходимости."
                ),
                "parts": resolved,
            }
        return {
            "ok": False,
            "error": f"код {code!r} не найден",
            "hint": "Проверьте написание через udc_search (префикс кода или слова из названия).",
        }

    # if both sources know the code but describe it very differently, warn:
    # teacode follows the old Russian print edition, summary the current UDC
    warnings = []
    if len(entries) == 2:
        t_a = entries[0].get("title", "")
        t_b = entries[1].get("title", "")
        if t_a and t_b and difflib.SequenceMatcher(None, t_a.casefold(), t_b.casefold()).ratio() < 0.5:
            warnings.append(
                "Источники по-разному раскрывают этот код: teacode основан на старом "
                "русском издании УДК (особенно устарели разделы 2 'Религия', 60, 79), "
                "summary — на актуальной редакции UDC Consortium. При расхождении "
                "предпочитайте summary и сверьтесь с udc_children."
            )
    if warnings:
        return {"ok": True, "code": code, "entries": entries, "warnings": warnings}
    return {"ok": True, "code": code, "entries": entries}


@mcp.tool()
def udc_children(code: str, source: str = "all", limit: int = 100) -> dict:
    """Прямые дочерние коды УДК (один уровень вниз). Удобно для обзора раздела."""
    srcs, err = resolve_sources(source)
    if err:
        return {"ok": False, "error": err}
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "пустой код (для корня используйте '')"}
    limit = max(1, min(int(limit), MAX_CHILDREN))
    out = {}
    total = 0
    for s in srcs:
        if code == "":
            kids = [k for k in s.children.get("", [])]
        else:
            kids = s.children.get(code, [])
            if not kids and not s.first(code):
                out[s.name] = {"error": "код не найден"}
                continue
        out[s.name] = {
            "children": [
                {
                    "code": c,
                    "title": s.rec_title(s.first(c) or {}),
                    "has_children": s.has_children(c),
                }
                for c in kids[:limit]
            ],
            "total": len(kids),
            "truncated": max(0, len(kids) - limit),
        }
        total += len(kids)
    return {"ok": True, "code": code, "sources": out}


@mcp.tool()
def udc_suggest(text: str, source: str = "all", limit: int = 8) -> dict:
    """Подбор кодов УДК по свободному тексту (тема статьи, аннотация, ключевые слова).

    Оценивает покрытие текста взвешенными терминами заголовков УДК; возвращает топ-коды
    с цепочкой предков (path) для выбора уровня детализации. Для составного кода
    объедините лучшие независимые коды знаком ':' или '+', см. примечание в ответе.
    """
    srcs, err = resolve_sources(source)
    if err:
        return {"ok": False, "error": err}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "пустой текст"}
    limit = max(1, min(int(limit), 20))
    q_tokens = tokens(text)
    if not q_tokens:
        return {
            "ok": False,
            "error": "в тексте нет содержательных терминов (только стоп-слова)",
            "stopwords_dropped": len(text.split()),
        }
    out = {}
    best_overall: list[dict] = []
    for s in srcs:
        idf_sum = sum(s.idf.get(t, 1.0) for t in q_tokens)
        scored = []
        for i, r in enumerate(s.recs):
            sc = _match_score(s, i, q_tokens, "", idf_sum)
            if sc >= 0.15:
                scored.append((sc, r["code"], i))
        scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        top = []
        for sc, code, i in scored[:limit]:
            r = s.recs[i]
            path = s.ancestors(code)
            top.append(
                {
                    "code": code,
                    "title": s.rec_title(r),
                    "source": s.name,
                    "score": round(sc, 3),
                    "matched_terms": [q for q in q_tokens if stem(q) in s.title_tokens[i]][:8],
                    "path": " > ".join(f"{a['code']} {a['title']}" for a in path if a["title"]) or None,
                    "has_children": s.has_children(code),
                    **({"cancelled": True} if r.get("cancelled") else {}),
                }
            )
        out[s.name] = {"suggestions": top}
        best_overall.extend(top)
    best_overall.sort(key=lambda x: -x["score"])
    return {
        "ok": True,
        "terms": q_tokens[:20],
        "suggestions": out,
        "top": best_overall[:limit],
        "note": (
            "Проверьте уровень детализации: широкому обзору соответствует верхний код path, "
            "узкой теме — сам код. Составные коды: 'A:B' — отношение (например '004.9:621.372'), "
            "'A+B' — объединение, '(A...)'/'=...'/'-02' — определители места/языка/лиц и т.п. "
            "Исключённые (cancelled) коды не используйте."
        ),
    }


def main() -> None:
    import sys

    for s in SOURCES.values():
        s.load()
    loaded = [n for n, s in SOURCES.items() if s.loaded]
    print(f"RuUDC MCP server ready. sources loaded: {loaded or 'none'}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
