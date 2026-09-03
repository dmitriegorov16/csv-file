#!/usr/bin/env python3
"""
Подбор рабочего запроса к каталогу Avito.

В бандле нашлось: requestCatalog -> GET /web/1/js/items. Это тот самый
запрос, которым сайт получает выдачу поиска в JSON — пачку объявлений
разом, а не по одному. Рядом лежит облегчённый режим countOnly=1,
возвращающий только счётчик: им дёшево проверять, отвечает ли эндпоинт
вообще.

Путь без параметров фаервол пропускает, а с выдуманным itemId даёт 403,
поэтому вопрос один: с какими параметрами он отвечает данными. Перебираем
осмысленные наборы и смотрим.

    python try_catalog.py
    python try_catalog.py --save data/catalog.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.avito.ru"

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# locationId 637640 — Москва, categoryId 9 — автомобили; это самые
# обычные значения, какие шлёт браузер при обычном просмотре каталога.
VARIANTS = [
    ("только счётчик", "/web/1/js/items?categoryId=9&locationId=637640&countOnly=1"),
    ("каталог, 50 штук",
     "/web/1/js/items?categoryId=9&locationId=637640&page=1&limit=50&display=list"),
    ("каталог без категории",
     "/web/1/js/items?locationId=637640&page=1&limit=50&display=list"),
    ("каталог с запросом",
     "/web/1/js/items?query=%D0%B0%D0%BA%D0%B2%D0%B0%D1%80%D0%B8%D1%83%D0%BC"
     "&locationId=637640&page=1&limit=50"),
    ("long-вариант",
     "/web/1/long/js/items?categoryId=9&locationId=637640&page=1&limit=50"),
    ("страница каталога", "/js/catalogpage?categoryId=9&locationId=637640"),
    ("аквариумы, вся Россия",
     "/web/1/js/items?categoryId=98&page=1&limit=50&display=list"),
]

HEADER_SETS = [
    ("как XHR", {"User-Agent": DESKTOP_UA, "Accept": "application/json",
                 "X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/moskva/avtomobili",
                 "Accept-Encoding": "gzip"}),
    ("как обычный запрос", {"User-Agent": DESKTOP_UA, "Accept": "*/*",
                            "Accept-Encoding": "gzip"}),
]


def get(path: str, headers: dict, timeout: int = 30):
    request = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return response.status, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return exc.code, raw
    except Exception as exc:
        return 0, type(exc).__name__.encode()


def describe(status: int, raw: bytes) -> str:
    head = raw[:200].decode("utf-8", "replace")
    if "<html" in head.lower() or "<!DOCTYPE" in head:
        return "страница фаервола" if status in (403, 429, 439) else f"HTML {status}"
    if status == 200:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return f"200, но не JSON: {head[:120]!r}"
        # что внутри — интересует прежде всего список объявлений
        if isinstance(data, dict):
            keys = ", ".join(list(data)[:8])
            items = data.get("items") or (data.get("result") or {}).get("items")
            count = len(items) if isinstance(items, list) else "—"
            return f"JSON, ключи: {keys} | объявлений: {count}"
        return f"JSON {type(data).__name__}, {len(raw)} байт"
    return f"{status}: {head[:100]!r}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save", default="",
                        help="куда сохранить первый удачный ответ целиком")
    args = parser.parse_args()

    saved = False
    for label, path in VARIANTS:
        print(f"\n{label}")
        for header_name, headers in HEADER_SETS:
            status, raw = get(path, headers)
            verdict = describe(status, raw)
            print(f"  {header_name:<20} {status:>3}  {verdict}")
            if args.save and not saved and verdict.startswith("JSON"):
                Path(args.save).write_bytes(raw)
                print(f"       ответ целиком сохранён в {args.save} "
                      f"({len(raw)} байт)")
                saved = True


if __name__ == "__main__":
    main()
