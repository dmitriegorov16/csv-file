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

from fast_scrape import block_text, meta_content, parse_html


def page_url(page_html: str) -> str:
    """Канонический адрес страницы — из og:url или <link rel=canonical>."""
    url = meta_content(page_html, "property", "og:url")
    if url:
        return url
    match = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        page_html, re.IGNORECASE)
    return match.group(1) if match else ""


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/empty_200.html")
    if not path.exists():
        sys.exit(f"Нет файла {path}")

    page_html = path.read_text(encoding="utf-8", errors="replace")
    print(f"{path}: {len(page_html)} символов\n")

    # Настоящий URL, а не заглушка: город и категория берутся в том числе
    # из ссылки, и с "https://www.avito.ru/x_1" они выходили бессмысленными.
    url = page_url(page_html) or "https://www.avito.ru/x_1"
    print(f"ссылка: {url}\n")
    listing = parse_html(page_html, url, 1)
    filled = 0
    for name, value in asdict(listing).items():
        text = " ".join(str(value).split())
        shown = (text[:80] + "…") if len(text) > 80 else text
        mark = "OK   " if text and name not in ("id", "url") else "ПУСТО"
        if text and name not in ("id", "url"):
            filled += 1
        print(f"  {mark} {name:12} {shown}")

    print(f"\nЗаполнено полей: {filled}/8")

    if filled < 8:
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

        # og:-теги целиком: их немного, а видно сразу, что страница отдала
        for name, value in re.findall(
                r'<meta[^>]+property=["\']og:([^"\']+)["\'][^>]+content=["\']([^"\']*)',
                page_html, re.IGNORECASE)[:12]:
            print(f"  og:{name:<12} {value[:70]}")

        # маркеры, где может лежать адрес — самое неочевидное поле
        near_address = sorted({m for m in re.findall(r'data-marker="([^"]{3,60})"', page_html)
                               if re.search(r"address|geo|location|delivery|seller",
                                            m, re.IGNORECASE)})
        if near_address:
            print("\n  маркеры, похожие на адрес:")
            for name in near_address:
                text = " ".join(block_text(page_html, name).split())[:70]
                print(f"    {name:<40} {text}")

        marker = re.findall(r'data-marker="([^"]{3,40})"', page_html)
        if marker:
            unique = sorted(set(marker))[:25]
            print(f"\n  найденные data-marker ({len(set(marker))} разных), первые:")
            for name in unique:
                print(f"    {name}")


if __name__ == "__main__":
    main()
