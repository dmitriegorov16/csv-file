#!/usr/bin/env python3
"""
Настоящие маршруты API — из JS самого Avito.

Проверка префиксов показала, что фаервол закрывает ровно один сегмент
пути: items. Всё остальное под /web/N/ доходит до роутера и честно
отвечает "route not found". Значит открытые маршруты есть, и вопрос
только в том, как они называются.

Подбирать имена наугад бессмысленно, а знать их точно можно: фронтенд
Avito сам ходит по этим адресам, и они записаны прямым текстом в его
JS-бандлах. Бандлы лежат на www.avito.st — это CDN со статикой, фаервола
там нет, качается с любого IP.

    python find_routes.py data/empty_200.html
    python find_routes.py data/empty_200.html --max 12
"""

from __future__ import annotations

import argparse
import gzip
import re
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_RE = re.compile(r'<script[^>]+src=["\'](https://[^"\']+\.js)["\']',
                       re.IGNORECASE)
LINK_RE = re.compile(r'["\'](https://www\.avito\.st/[^"\']+\.js)["\']')

# Пути вида /web/1/foo/bar — в JS они лежат и целыми строками, и кусками,
# которые склеиваются в рантайме, поэтому ищем и то, и другое.
ROUTE_RE = re.compile(r'/(?:web|js|api)/\d+(?:/[a-zA-Z0-9_.-]{2,40}){1,4}')
PARTIAL_RE = re.compile(r'["\'](/(?:web|js|api)/[a-zA-Z0-9_./-]{4,60})["\']')

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0.0.0 Safari/537.36",
           "Accept-Encoding": "gzip"}


def download(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("page", nargs="?", default="data/empty_200.html")
    parser.add_argument("--max", type=int, default=8,
                        help="сколько бандлов скачать")
    args = parser.parse_args()

    path = Path(args.page)
    if not path.exists():
        raise SystemExit(f"Нет файла {path}")
    page = path.read_text(encoding="utf-8", errors="replace")

    scripts = list(dict.fromkeys(SCRIPT_RE.findall(page) + LINK_RE.findall(page)))
    print(f"скриптов на странице: {len(scripts)}")
    if not scripts:
        raise SystemExit("Ссылок на JS не нашлось — не тот файл?")

    routes: dict = {}
    total_bytes = 0
    for number, url in enumerate(scripts[:args.max], 1):
        name = url.rsplit("/", 1)[-1]
        try:
            body = download(url)
        except Exception as exc:
            print(f"  [{number}] {name[:50]}: {type(exc).__name__}")
            continue
        total_bytes += len(body)
        found = set(ROUTE_RE.findall(body)) | set(PARTIAL_RE.findall(body))
        for route in found:
            routes.setdefault(route, set()).add(name)
        print(f"  [{number}] {name[:50]}: {len(body) // 1024} КБ, "
              f"маршрутов {len(found)}")

    print(f"\nвсего скачано {total_bytes // 1024} КБ, "
          f"разных маршрутов {len(routes)}\n")

    # Сначала то, что похоже на карточку объявления: ради этого всё и затевалось.
    interesting = [r for r in routes
                   if re.search(r"item|iva|card|advert|offer", r, re.IGNORECASE)]
    print("похожие на карточку объявления:")
    for route in sorted(interesting)[:40]:
        print(f"  {route}")
    if not interesting:
        print("  —")

    print("\nостальные (первые 40):")
    for route in sorted(set(routes) - set(interesting))[:40]:
        print(f"  {route}")


if __name__ == "__main__":
    main()
