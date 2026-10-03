#!/usr/bin/env python3
"""Токенизация/нормализация УДК-текста и ключ шарда.

Выделено в отдельный модуль, чтобы scripts/build_shards.py считал vocab
той же логикой, что и рантайм сервера, без зависимости от mcp.
"""

from __future__ import annotations

import re

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


def shard_key(code: str) -> str:
    """Ключ шарда = первые два символа кода (верхний раздел УДК: 00–99, корни)."""
    return code[:2]
