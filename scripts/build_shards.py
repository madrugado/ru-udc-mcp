#!/usr/bin/env python3
"""Разбить монолитные data/*.json на ленивые шарды в data/shards/<источник>/.

Шард = записи с одинаковыми первыми двумя символами кода (для УДК это верхний
раздел: 00–99, плюс односимвольные корни). Для каждого источника создаётся:

  data/shards/<src>/meta.json        — метаданные, список шардов, рёбра корней
                                        (дети "" и односимвольных кодов)
  data/shards/<src>/vocab.json       — стем термина -> [ключи шардов]
                                        (маршрутизация текстового поиска)
  data/shards/<src>/shards/<key>.json — записи шарда

Сервер при старте читает только meta+vocab и подгружает шарды по запросу
(udc_get/udc_children — шарды по префиксу кода; udc_search/udc_suggest — шарды
из vocab по терминам запроса). Монолитные data/*.json остаются каноническими
снимками; этот скрипт — обязательный пост-шаг после обновления данных.

Запуск: python3 scripts/build_shards.py [источники...]   (по умолчанию все)
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from udctext import shard_key, stem, tokens  # noqa: E402 — та же токенизация, что в рантайме

DATA = ROOT / "data"
MONOLITHS = {
    "teacode": DATA / "teacode_udc.json",
    "triumph": DATA / "triumph_udc.json",
    "summary": DATA / "udcsummary_ru.json",
}


def build(name: str, mono: Path) -> None:
    blob = json.loads(mono.read_text(encoding="utf-8"))
    meta = dict(blob.get("meta", {}))
    records = blob["records"]

    out = DATA / "shards" / name
    if out.exists():
        shutil.rmtree(out)
    (out / "shards").mkdir(parents=True)

    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(shard_key(r["code"]), []).append(r)

    vocab: dict[str, set[str]] = {}
    df: dict[str, int] = {}  # глобальные частоты терминов — для сопоставимого IDF между шардами
    for key, recs in groups.items():
        for r in recs:
            for t in {stem(x) for x in tokens(r.get("title") or "")}:
                vocab.setdefault(t, set()).add(key)
                df[t] = df.get(t, 0) + 1
        (out / "shards" / f"{quote(key, safe='')}.json").write_text(
            json.dumps({"records": recs}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    # верхний уровень иерархии не помещается в один шард (дети "6" живут в "60".."69"),
    # поэтому рёбра корней храним в meta
    top: dict[str, list[str]] = {
        "": sorted({r["code"] for r in records if not (r.get("parent") or r.get("parent_hint"))})
    }
    for r in records:
        p = r.get("parent") or r.get("parent_hint") or ""
        if len(p) == 1 and p != "":
            top.setdefault(p, set()).add(r["code"])
    top = {k: sorted(v) for k, v in top.items() if v}

    meta.update(
        {
            "records": len(records),
            "shard_keys": sorted(groups),
            "top_children": top,
            "idf": {t: math.log(1.0 + len(records) / c) for t, c in df.items()},
        }
    )
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out / "vocab.json").write_text(
        json.dumps({t: sorted(ks) for t, ks in vocab.items()}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    total = sum((out / "shards" / f"{quote(k, safe='')}.json").stat().st_size for k in groups)
    print(
        f"{name}: {len(records)} записей -> {len(groups)} шардов "
        f"({total / 1e6:.1f} МБ), vocab {len(vocab)} терминов"
    )


def main() -> None:
    names = sys.argv[1:] or list(MONOLITHS)
    for n in names:
        if n not in MONOLITHS:
            print(f"неизвестный источник {n!r}; доступны: {', '.join(MONOLITHS)}")
            sys.exit(2)
        if not MONOLITHS[n].exists():
            print(f"{n}: пропущен — нет {MONOLITHS[n].name}")
            continue
        build(n, MONOLITHS[n])


if __name__ == "__main__":
    main()
