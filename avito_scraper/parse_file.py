#!/usr/bin/env python3
"""
Разобрать сохранённую страницу парсером — без сети.

Нужен, когда страница уже скачана (например, попала в
data/empty_200.html), и надо понять, что из неё вытаскивается и чего не
хватает. Это отделяет вопрос «пускает ли нас Avito» от вопроса «умеем ли
мы разобрать то, что он прислал»: второй решается локально и бесплатно.

    python parse_file.py data/empty_200.html
"""

from __future__ import annotations

import re
import sys
from dataclasses import asdict
from pathlib import Path

from fast_scrape import parse_html


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/empty_200.html")
    if not path.exists():
        sys.exit(f"Нет файла {path}")

    page_html = path.read_text(encoding="utf-8", errors="replace")
    print(f"{path}: {len(page_html)} символов\n")

    listing = parse_html(page_html, "https://www.avito.ru/x_1", 1)
    filled = 0
    for name, value in asdict(listing).items():
        text = " ".join(str(value).split())
        shown = (text[:80] + "…") if len(text) > 80 else text
        mark = "OK   " if text and name not in ("id", "url") else "ПУСТО"
        if text and name not in ("id", "url"):
            filled += 1
        print(f"  {mark} {name:12} {shown}")

    print(f"\nЗаполнено полей: {filled}/8")

    if filled < 4:
        # Подсказки, откуда брать данные, если привычные места пусты:
        # у Avito страница может отдаваться как SPA, и всё содержимое
        # лежит в JSON внутри <script>, а не в разметке.
        print("\nЧто есть в странице (по чему можно зацепиться):")
        checks = [
            ("og:-теги", r'property=["\']og:'),
            ("JSON-LD", r'application/ld\+json'),
            ("data-marker", r'data-marker='),
            ("__initialData__", r'__initialData__'),
            ("window.__PRELOADED", r'__PRELOADED'),
            ("itemprop", r'itemprop='),
            ('"price"', r'"price"'),
            ('"title"', r'"title"'),
        ]
        for label, pattern in checks:
            count = len(re.findall(pattern, page_html))
            print(f"  {count:>6}  {label}")

        marker = re.findall(r'data-marker="([^"]{3,40})"', page_html)
        if marker:
            unique = sorted(set(marker))[:25]
            print(f"\n  найденные data-marker ({len(set(marker))} разных), первые:")
            for name in unique:
                print(f"    {name}")


if __name__ == "__main__":
    main()
