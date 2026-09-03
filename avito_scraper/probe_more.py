#!/usr/bin/env python3
"""
Где ещё лежат цена и описание, кроме страницы объявления.

Две проверки, обе бесплатные и без прокси:

  1) какие вообще карты есть в sitemap-индексе. Мы брали только item_*,
     а их 4279 — вдруг есть карты с большим числом полей.
  2) отвечает ли внутренний API Avito. Страница грузит данные обычным
     JSON-запросом, и он весит килобайты вместо 700 КБ. Если API открыт,
     весь расчёт по трафику и прокси меняется в двадцать раз.

Читать результат так:

    404          адрес не тот, но фаервол пропустил — ищем правильный
    429 / 439    API закрыт так же, как страницы
    200          выиграли

    python probe_more.py
    python probe_more.py --id 8245305594
"""

from __future__ import annotations

import argparse
import collections
import re
import urllib.error
import urllib.request

INDEX = "https://www.avito.ru/sitemap/index.xml"
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

# Ключ мобильного приложения — он не секрет, приложение шлёт его в
# открытую в каждом запросе; мы видели его в живом трафике.
APP_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

# Пробуем не только пути, но и хосты: приложение ходит на m.avito.ru, а
# мы до сих пор стучались только в www. И с ключом, без которого API
# отвечать не обязан.
ENDPOINTS = [
    "https://m.avito.ru/api/16/items/{id}?key=" + APP_KEY,
    "https://m.avito.ru/api/14/items/{id}?key=" + APP_KEY,
    "https://m.avito.ru/api/1/items/{id}?key=" + APP_KEY,
    "https://api.avito.ru/api/16/items/{id}?key=" + APP_KEY,
    "https://www.avito.ru/api/16/items/{id}?key=" + APP_KEY,
    "https://m.avito.ru/web/1/items/{id}",
    # 429 на этом маршруте означает, что он существует: фаервол закрывает
    # то, что есть, а не то, чего нет
    "https://www.avito.ru/web/1/items/{id}",
    "https://www.avito.ru/web/1/item/{id}",
    "https://www.avito.ru/web/3/item/{id}",
    "https://www.avito.ru/web/1/items/{id}/summary",
    "https://www.avito.ru/web/1/items/{id}/description",
]


def show_sitemap_kinds() -> None:
    request = urllib.request.Request(INDEX, headers={"User-Agent": IPHONE_UA})
    with urllib.request.urlopen(request, timeout=40) as response:
        index = response.read().decode("utf-8", "replace")
    maps = re.findall(r"<loc>([^<]+)</loc>", index)
    print(f"карт в индексе: {len(maps)}")
    kinds = collections.Counter(
        re.sub(r"[_\d]+\.xml.*", "", m.rsplit("/", 1)[-1]) for m in maps)
    for kind, count in kinds.most_common(20):
        print(f"  {count:>5}  {kind}")


def probe(item_id: str) -> None:
    print("\nвнутренний API:")
    for template in ENDPOINTS:
        path = template.format(id=item_id)
        shown = path.split("?")[0].replace("https://", "")
        request = urllib.request.Request(
            path, headers={"User-Agent": IPHONE_UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body = response.read(400)
            print(f"  {shown:<45} {response.status}  {body[:150]!r}")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(200)
            except Exception:
                body = b""
            print(f"  {shown:<45} {exc.code}  {body[:120]!r}")
        except Exception as exc:
            print(f"  {shown:<45} {type(exc).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", default="8245305594")
    parser.add_argument("--only-api", action="store_true",
                        help="не перечислять карты sitemap, они уже известны")
    args = parser.parse_args()
    if not args.only_api:
        show_sitemap_kinds()
    probe(args.id)


if __name__ == "__main__":
    main()
