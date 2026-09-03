#!/usr/bin/env python3
"""
Структура одного объявления из ответа каталога.

Каталог отдаёт готовый JSON, и раскладывать его по колонкам надо по
фактическим именам полей, а не по догадкам: priceDetailed, urlPath,
addressDetailed — это не то же самое, что price, url и address.

    python show_item.py data/catalog.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def walk(value, prefix: str = "", depth: int = 0):
    """Плоский список путей и значений — так видно всё сразу."""
    pad = "  " * depth
    if isinstance(value, dict):
        for key, inner in value.items():
            if isinstance(inner, (dict, list)):
                print(f"{pad}{key}:")
                walk(inner, f"{prefix}.{key}", depth + 1)
            else:
                text = str(inner)
                print(f"{pad}{key} = {text[:110]}")
    elif isinstance(value, list):
        print(f"{pad}[{len(value)} шт.]")
        if value:
            walk(value[0], prefix, depth + 1)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/catalog.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"ключи ответа: {', '.join(list(data)[:20])}\n")
    items = data.get("items")
    if not isinstance(items, list):
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                items = value["items"]
                break
    if not items:
        print("список объявлений не нашёлся")
        return

    print(f"объявлений: {len(items)}")
    # Первое объявление — целиком: по нему пишется раскладка в CSV.
    print("\n--- первое объявление целиком:")
    walk(items[0])

    # Заодно проверим, все ли одинаковы: если у части нет цены или фото,
    # это надо знать заранее, а не на 20000-й строке.
    print("\n--- сколько объявлений имеет поле:")
    keys = {}
    for item in items:
        if isinstance(item, dict):
            for key in item:
                keys[key] = keys.get(key, 0) + 1
    for key, count in sorted(keys.items(), key=lambda kv: -kv[1]):
        mark = "" if count == len(items) else "  <-- не у всех"
        print(f"  {count:>4}/{len(items)}  {key}{mark}")


if __name__ == "__main__":
    main()
