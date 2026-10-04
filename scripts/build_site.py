#!/usr/bin/env python3
"""Синхронизация шардов в site/data для статического деплоя (tatnet и др.).

Шард-файлы в data/shards названы URL-энкодом ключа (quote(key, safe='')),
а статические хостинги декодируют путь запроса — имена бы сломались.
Поэтому здесь каждый шард переименовывается в hex от ключа, JS считает
те же имена (TextEncoder → hex).

Хостинг не сжатие не делает, поэтому рядом с каждым .json кладётся
.json.gz — клиент предпочитает его (DecompressionStream) и падает
на несжатый файл, если браузер старый.
"""

from __future__ import annotations

import gzip
import shutil
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "shards"
DST = ROOT / "site" / "data" / "shards"


def put(dst: Path, data: bytes) -> None:
    dst.write_bytes(data)
    dst.with_suffix(dst.suffix + ".gz").write_bytes(gzip.compress(data, compresslevel=9, mtime=0))


def main() -> None:
    if not SRC.is_dir():
        sys.exit("нет data/shards — сначала запустите scripts/build_shards.py")
    if DST.exists():
        shutil.rmtree(DST)
    total = 0
    for src_dir in sorted(SRC.iterdir()):
        if not src_dir.is_dir():
            continue
        out = DST / src_dir.name
        (out / "shards").mkdir(parents=True)
        for name in ("meta.json", "vocab.json"):
            put(out / name, (src_dir / name).read_bytes())
        for p in sorted((src_dir / "shards").glob("*.json")):
            key = urllib.parse.unquote(p.stem)
            put(out / "shards" / f"{key.encode('utf-8').hex()}.json", p.read_bytes())
            total += 1
        print(f"{src_dir.name}: meta+vocab + {len(list((src_dir / 'shards').glob('*.json')))} шардов")
    print(f"site/data готов ({total} шардов, каждый в .json и .json.gz)")


if __name__ == "__main__":
    main()
