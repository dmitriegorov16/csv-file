#!/usr/bin/env python3
"""
Сколько байт страницы надо скачать, чтобы поля разобрались.

Открытого API у Avito не нашлось, значит данные берутся со страницы, а
она весит около 700 КБ. На 30000 объявлений это 21 ГБ — больше, чем даёт
любой разумный пакет мобильных прокси. Но целиком страница и не нужна:
в коде есть --head-bytes, читающий только начало.

Скрипт режет уже сохранённую страницу на префиксы и смотрит, какие поля
разбираются от каждого. Ответ получается точный и бесплатный: сеть не
нужна, страница уже есть.

    python head_probe.py data/empty_200.html
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from fast_scrape import parse_html

STEPS = [20, 40, 60, 80, 100, 150, 200, 300, 400, 500, 700]
WATCH = ("title", "content", "description", "image", "price", "category",
         "city", "address")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/empty_200.html")
    if not path.exists():
        sys.exit(f"Нет файла {path}")
    page = path.read_text(encoding="utf-8", errors="replace")
    url = "https://www.avito.ru/irkutsk/avtomobili/kia_sportage_8245305594"

    full = asdict(parse_html(page, url, 1))
    filled_full = {name for name in WATCH if full.get(name)}
    print(f"{path.name}: {len(page) // 1024} КБ, "
          f"целиком разбирается полей: {len(filled_full)}\n")

    print(f"{'КБ':>5}  {'полей':>5}  чего не хватает")
    best = None
    for kilobytes in STEPS:
        piece = page[:kilobytes * 1024]
        if len(piece) == len(page) and best is not None:
            break
        try:
            got = asdict(parse_html(piece, url, 1))
        except Exception as exc:
            print(f"{kilobytes:>5}  сбой разбора: {type(exc).__name__}")
            continue
        filled = {name for name in WATCH if got.get(name)}
        missing = sorted(filled_full - filled)
        print(f"{kilobytes:>5}  {len(filled):>5}  "
              f"{', '.join(missing) if missing else '— всё на месте'}")
        if not missing and best is None:
            best = kilobytes

    print()
    if best:
        total = best * 30000 / 1024 / 1024
        print(f"Достаточно {best} КБ на карточку.")
        print(f"На 30000 объявлений это ~{total:.1f} ГБ "
              f"вместо ~{len(page) * 30000 / 1024 / 1024 / 1024:.0f} ГБ.")
    else:
        print("Ни один префикс не дал полного набора — нужна страница целиком.")
        print("Значит экономить трафик обрезкой не выйдет, "
              "и считать надо по полному весу.")


if __name__ == "__main__":
    main()
