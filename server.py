#!/usr/bin/env python3
"""RuUDC — MCP-сервер для УДК (универсальной десятичной классификации).

Источники (ленивые шарды в data/shards/<name>/, монолиты — канонические снимки):
  * triumph   — ОСНОВНОЙ: «УДК Классификатор 2026» изд. Триумф (современная редакция);
  * summary   — официальный UDC Summary (UDC Consortium, CC BY-NC-ND; вспом. таблицы);
  * teacode   — детальный справочник teacode.com (121k кодов, издание ~2015, местами устарел).

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
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

DATA_DIR = Path(os.environ.get("UDC_DATA_DIR") or Path(__file__).resolve().parent / "data")
MAX_CHILDREN = 500

mcp = MCPServer(
    "ruudc",
    instructions=(
        "Сервер УДК (Universal Decimal Classification). Ищите коды через udc_search / "
        "udc_suggest, смотрите иерархию через udc_get и udc_children. Основной источник — "
        "'triumph' (современная редакция 2026, выдаётся первым во всех ответах): "
        "давайте его коды, если источники расходятся. 'summary' — официальный свод UDC "
        "Consortium (единственный со вспомогательными таблицами определителей), "
        "'teacode' — детальные таблицы старого русского издания (самая большая глубина, "
        "но местами устаревшие формулировки; помечен cancelled не использовать). "
        "УДК-композиции (уточнение темы) строятся "
        "знаками ':' (отношение), '+' (объединение), '/' (диапазон) из простых кодов."
    ),
)

# ---------------------------------------------------------------- text utils
# (нормализация/стемминг/токены и shard_key живут в udctext.py — их же
# использует scripts/build_shards.py, чтобы vocab строился той же логикой)

from udctext import STOP, norm, shard_key, stem, tokens  # noqa: F401 — re-export

# ---------------------------------------------------------------- data model


class ShardData:
    """Индексы одного шарда (или целого монолитного датасета)."""

    __slots__ = ("recs", "by_code", "children", "norm_texts", "stem_texts", "title_tokens", "idf")

    def __init__(self, recs: list[dict]):
        self.recs = recs
        self.by_code: dict[str, list[dict]] = {}
        self.children: dict[str, list[str]] = {}
        self.norm_texts: list[str] = []
        self.stem_texts: list[str] = []
        self.title_tokens: list[frozenset[str]] = []
        df: Counter = Counter()
        for r in recs:
            code = r["code"]
            self.by_code.setdefault(code, []).append(r)
            parent = r.get("parent") or r.get("parent_hint") or ""
            self.children.setdefault(parent, []).append(code)
            title = r.get("title") or ""
            notes = r.get("note") or r.get("notes") or ""
            self.norm_texts.append(norm(title) + " | " + norm(notes))
            toks = frozenset(stem(t) for t in tokens(title))
            self.title_tokens.append(toks)
            self.stem_texts.append(" ".join(stem(t) for t in tokens(title + " " + notes)))
            for t in toks:
                df[t] += 1
        n = max(len(recs), 1)
        self.idf = {t: math.log(1.0 + n / c) for t, c in df.items()}


class Source:
    """Источник УДК с ленивой загрузкой.

    Приоритет — шардированный вариант data/shards/<name>/ (быстрый старт:
    читаются только meta.json и vocab.json, шарды подгружаются по запросу —
    по префиксу кода или через vocab по терминам). Если шардов нет, монолитный
    data/<name>.json грузится целиком (медленнее, но работает; после обновления
    данных запускайте scripts/build_shards.py).
    """

    def __init__(self, name: str, mono_path: Path, shards_dir: Path):
        self.name = name
        self.mono_path = mono_path
        self.shards_dir = shards_dir
        self.meta: dict = {}
        self.vocab: dict[str, list[str]] | None = None
        self.shard_keys: list[str] = []
        self.top_children: dict[str, list[str]] = {}
        self.shards: dict[str, ShardData] = {}
        self.single: ShardData | None = None
        self.idf_global: dict[str, float] = {}  # IDF по всему датасету (для сравнимости шардов)
        self.loaded = False
        self.error = ""

    @property
    def sharded(self) -> bool:
        return self.shard_keys and not self.single

    @property
    def records_count(self) -> int:
        if isinstance(self.meta.get("records"), int):
            return self.meta["records"]
        return len(self.single.recs) if self.single else 0

    def load(self) -> None:
        if self.loaded or self.error:
            return
        meta_p = self.shards_dir / "meta.json"
        if meta_p.exists():
            try:
                self.meta = json.loads(meta_p.read_text(encoding="utf-8"))
                self.shard_keys = self.meta.get("shard_keys", [])
                self.top_children = self.meta.get("top_children", {})
                self.idf_global = self.meta.get("idf", {})
                vocab_p = self.shards_dir / "vocab.json"
                self.vocab = (
                    json.loads(vocab_p.read_text(encoding="utf-8")) if vocab_p.exists() else {}
                )
                self.loaded = True
                return
            except Exception as exc:  # noqa: BLE001
                self.error = f"не удалось прочитать шарды {self.shards_dir.name}: {exc}"
                return
        if self.mono_path.exists():
            try:
                blob = json.loads(self.mono_path.read_text(encoding="utf-8"))
                self.meta = blob.get("meta", {})
                self.single = ShardData(blob.get("records", []))
                self.idf_global = self.single.idf  # весь датасет в одном «шарде»
                self.loaded = True
                return
            except Exception as exc:  # noqa: BLE001
                self.error = f"не удалось прочитать {self.mono_path.name}: {exc}"
                return
        self.error = (
            f"нет данных: ни {self.shards_dir.name}/, ни {self.mono_path.name} "
            "(запустите scripts/scrape_*.py и scripts/build_shards.py)"
        )

    # --- доступ к данным (лениво) ----------------------------------------

    def shard(self, key: str) -> ShardData:
        d = self.shards.get(key)
        if d is None:
            path = self.shards_dir / "shards" / f"{quote(key, safe='')}.json"
            d = ShardData(json.loads(path.read_text(encoding="utf-8"))["records"])
            self.shards[key] = d
        return d

    def data_for(self, code: str) -> ShardData | None:
        """Данные, в которых лежит сам код (и его дети)."""
        if self.single is not None or not self.shard_keys:
            return self.single
        key = shard_key(code)
        return self.shard(key) if key in self.shard_keys else None

    def first(self, code: str) -> dict | None:
        d = self.data_for(code)
        lst = d.by_code.get(code) if d else None
        return lst[0] if lst else None

    def children_of(self, code: str) -> list[str]:
        if self.single is not None or not self.shard_keys:
            return self.single.children.get(code, []) if self.single else []
        # дети корня ("6") живут в шардах "60".."69" — рёбра корней берём из meta
        if code == "" or len(code) <= 1:
            return self.top_children.get(code, [])
        key = shard_key(code)
        if key in self.shard_keys:
            return self.shard(key).children.get(code, [])
        return []

    def has_children(self, code: str) -> bool:
        return bool(self.children_of(code))

    def shards_for_prefix(self, prefix: str) -> list[str]:
        """Ключи шардов, где могут лежать коды с данным префиксом."""
        if not self.shard_keys:
            return []
        cut = prefix[:2]
        return [k for k in self.shard_keys if k.startswith(cut)]

    def shards_for_terms(self, stems: list[str]) -> list[str] | None:
        """Ключи шардов, где встречаются стемы запроса (None — ищем по всем)."""
        if not self.shard_keys or self.single is not None:
            return None
        keys: set[str] = set()
        for st in stems:
            keys.update(self.vocab.get(st, ()) if self.vocab else ())
        return sorted(keys)

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
            kids = self.children_of(r["code"])
            e["children"] = [
                {"code": c, "title": self.rec_title(self.first(c))} for c in kids[:MAX_CHILDREN]
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


# порядок = приоритет в ответах: triumph — основной источник (актуальная редакция),
# summary — официальный каркас и вспомогательные таблицы, teacode — глубина/устаревшие
SOURCES: dict[str, Source] = {
    "triumph": Source("triumph", DATA_DIR / "triumph_udc.json", DATA_DIR / "shards" / "triumph"),
    "summary": Source("summary", DATA_DIR / "udcsummary_ru.json", DATA_DIR / "shards" / "summary"),
    "teacode": Source("teacode", DATA_DIR / "teacode_udc.json", DATA_DIR / "shards" / "teacode"),
}

# приоритет источников при равном счёте и при дедупе одинаковых кодов
SOURCE_PRIO = {"triumph": 0, "summary": 1, "teacode": 2}


def resolve_sources(source: str) -> tuple[list[Source] | None, str | None]:
    if source == "all":
        wanted = list(SOURCES.values())
    elif source in SOURCES:
        wanted = [SOURCES[source]]
    else:
        return None, f"неизвестный источник: {source!r} (доступны: all, {', '.join(SOURCES)})"
    ok: list[Source] = []
    for s in wanted:
        s.load()
        if s.loaded:
            ok.append(s)
    if not ok:
        return None, "; ".join(f"источник {s.name}: {s.error}" for s in wanted)
    return ok, None


def unavailable_sources() -> list[str]:
    """Источники, перечисленные в SOURCES, но не загрузившиеся (файл отсутствует и т.п.)."""
    return [f"{s.name}: {s.error}" for s in SOURCES.values() if not s.loaded and s.error]


# ---------------------------------------------------------------- scoring


def _match_score(
    data: ShardData,
    i: int,
    q_tokens: list[str],
    phrase: str,
    idf_sum: float,
    idf_map: dict[str, float],
) -> float:
    text = data.norm_texts[i]
    stems = data.stem_texts[i]
    ttoks = data.title_tokens[i]
    score = 0.0
    for t in q_tokens:
        st = stem(t)
        w = idf_map.get(st) or data.idf.get(st, 1.0)
        if st in ttoks:
            score += w * 1.3
        elif st in stems:
            score += w
        elif t in text:
            score += w * 0.7
    if idf_sum > 0:
        score /= idf_sum
    if phrase and len(phrase) >= 4 and phrase in text:
        score += 0.5
    return score


def _scan_shard(
    src: Source, data: ShardData, q_tokens: list[str], phrase: str, idf_sum: float
) -> list[tuple[float, str, int]]:
    """Отсканировать один шард текстовым скорингом (idf — глобальный по источнику)."""
    scored = []
    for i in range(len(data.recs)):
        sc = _match_score(data, i, q_tokens, phrase, idf_sum, src.idf_global)
        if sc > 0:
            scored.append((sc, data.recs[i]["code"], i))
    return scored


def _search_in_source(src: Source, query: str, limit: int) -> tuple[list[str], list[dict]]:
    """Return (code_matches, text_matches) ranked."""
    # --- поиск по префиксу кода: только шарды с этим префиксом
    code_matches: list[str] = []
    q_norm = norm(query).replace(" ", "")
    if q_norm:
        if src.single is not None:
            for code in src.single.by_code:
                if code == q_norm or code.startswith(q_norm):
                    code_matches.append(code)
        else:
            for key in src.shards_for_prefix(q_norm):
                for code in src.shard(key).by_code:
                    if code == q_norm or code.startswith(q_norm):
                        code_matches.append(code)
        code_matches.sort(key=lambda c: (len(c), c))
        code_matches = code_matches[:limit]

    # --- текстовый поиск: маршрутизация по vocab (какие шарды содержат термины)
    q_tokens = tokens(query)
    phrase = norm(query)
    text_matches: list[dict] = []
    if q_tokens:
        stems = [stem(t) for t in q_tokens]
        idf_sum = sum(src.idf_global.get(st, 1.0) for st in stems)
        keys = src.shards_for_terms(stems)
        if keys is None:  # монолит — сканируем всё
            keys = ["*"]
        scored: list[tuple[float, str, int, ShardData]] = []
        for key in keys:
            data = src.single if key == "*" else src.shard(key)
            for sc, code, i in _scan_shard(src, data, q_tokens, phrase, idf_sum):
                scored.append((sc, code, i, data))
        scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        for sc, code, i, data in scored[:limit]:
            r = data.recs[i]
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
            "codes": s.records_count,
            "ok": s.loaded,
            "primary": s.name == next(iter(SOURCES)),
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
    source: 'all' | 'teacode' | 'summary' | 'triumph'. limit: максимум результатов на источник.
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
    skip = unavailable_sources()
    if skip:
        results["unavailable_sources"] = skip
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

    # if several sources know the code but describe it very differently, warn:
    # teacode follows the old Russian print edition, summary/triumph the current UDC
    warnings = []
    if len(entries) > 1:
        titles = [e.get("title", "") for e in entries]
        names = [e["source"] for e in entries]
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                if not (titles[i] and titles[j]):
                    continue
                ratio = difflib.SequenceMatcher(None, titles[i].casefold(), titles[j].casefold()).ratio()
                if ratio < 0.5:
                    warnings.append(
                        f"Источники '{names[i]}' и '{names[j]}' по-разному раскрывают этот код "
                        f"(«{titles[i][:60]}» vs «{titles[j][:60]}»). teacode основан на старом "
                        "русском издании УДК (особенно устарели разделы 2 'Религия', 60, 79). "
                        "Основной источник — triumph: используйте его трактовку; summary и "
                        "triumph отражают актуальную редакцию."
                    )
    if warnings:
        return {"ok": True, "code": code, "entries": entries, "warnings": warnings}
    resp = {"ok": True, "code": code, "entries": entries}
    skip = unavailable_sources()
    if skip:
        resp["unavailable_sources"] = skip
    return resp


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
        kids = s.children_of(code)
        if code != "" and not kids and not s.first(code):
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
    resp = {"ok": True, "code": code, "sources": out}
    skip = unavailable_sources()
    if skip:
        resp["unavailable_sources"] = skip
    return resp


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
        stems = [stem(t) for t in q_tokens]
        idf_sum = sum(s.idf_global.get(st, 1.0) for st in stems)
        keys = s.shards_for_terms(stems)
        if keys is None:  # монолит — сканируем всё
            keys = ["*"]
        scored: list[tuple[float, str, int, ShardData]] = []
        for key in keys:
            data = s.single if key == "*" else s.shard(key)
            for sc, code, i in _scan_shard(s, data, q_tokens, "", idf_sum):
                if sc >= 0.15:
                    scored.append((sc, code, i, data))
        scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        top = []
        for sc, code, i, data in scored[:limit]:
            r = data.recs[i]
            path = s.ancestors(code)
            top.append(
                {
                    "code": code,
                    "title": s.rec_title(r),
                    "source": s.name,
                    "score": round(sc, 3),
                    "matched_terms": [q for q in q_tokens if stem(q) in data.title_tokens[i]][:8],
                    "path": " > ".join(f"{a['code']} {a['title']}" for a in path if a["title"]) or None,
                    "has_children": s.has_children(code),
                    **({"cancelled": True} if r.get("cancelled") else {}),
                }
            )
        out[s.name] = {"suggestions": top}
        best_overall.extend(top)
    # общий топ: одинаковый код из нескольких источников показываем от приоритетного
    # (triumph) — кросс-источниковые счёты несравнимы из-за разных IDF, поэтому
    # при дедупе источник важнее счёта; порядок итога — по счёту выбранной записи
    best_by_code: dict[str, dict] = {}
    for item in best_overall:
        cur = best_by_code.get(item["code"])
        rank = (SOURCE_PRIO.get(item["source"], 9), -item["score"])
        if cur is None or rank < (SOURCE_PRIO.get(cur["source"], 9), -cur["score"]):
            best_by_code[item["code"]] = item
    merged_top = sorted(
        best_by_code.values(),
        key=lambda x: (-x["score"], SOURCE_PRIO.get(x["source"], 9), len(x["code"]), x["code"]),
    )[:limit]
    resp = {
        "ok": True,
        "terms": q_tokens[:20],
        "suggestions": out,
        "top": merged_top,
        "note": (
            "Основной источник — triumph (современная редакция 2026): его трактовка "
            "приоритетна, при равном счёте код показывается от него. Проверьте уровень "
            "детализации: широкому обзору соответствует верхний код path, узкой теме — сам "
            "код. Составные коды: 'A:B' — отношение (например '004.9:621.372'), "
            "'A+B' — объединение, '(A...)'/'=...'/'-02' — определители места/языка/лиц "
            "(есть только в summary). Исключённые (cancelled) коды не используйте."
        ),
    }
    skip = unavailable_sources()
    if skip:
        resp["unavailable_sources"] = skip
    return resp


def main() -> None:
    import sys

    for s in SOURCES.values():
        s.load()
    loaded = [n for n, s in SOURCES.items() if s.loaded]
    print(f"RuUDC MCP server ready. sources loaded: {loaded or 'none'}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
