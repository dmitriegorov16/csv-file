#!/usr/bin/env python3
"""
Проверка найденных маршрутов и подглядывание, как их зовёт сам сайт.

В бандле item.js нашлись настоящие адреса API. Два вопроса к каждому:
закрыт ли он фаерволом и с какими параметрами его вызывают. Первое
проверяется запросом, второе — чтением куска JS вокруг адреса.

Отдельно интересна позиция сегмента "items". Фаервол резал
/web/N/items/... , где items второй. В /web/1/js/items он третий — и это
может быть не одно и то же правило.

    python probe_routes.py                 # проверить адреса
    python probe_routes.py --show catalogpage   # как его зовут в JS
"""

from __future__ import annotations

import argparse
import gzip
import re
import urllib.error
import urllib.request

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

BUNDLE = "https://www.avito.st/s/desktop/item.58c395bb32b9b19a.js"

CANDIDATES = [
    "/web/1/js/items/{id}",
    "/web/1/js/items?itemId={id}",
    "/web/1/long/js/items/{id}",
    "/web/3/items/{id}/complementary",
    "/js/1/recommendations/main?itemId={id}",
    "/js/1/recommendations/more?itemId={id}",
    "/js/catalogpage",
    "/js/catalogpage?categoryId=9&locationId=637640",
    "/web/1/category/tree",
    "/js/v2/geo/position",
    "/web/1/header/favoritesCounts",
]


def classify(status: int, body: bytes) -> str:
    text = body[:300].decode("utf-8", "replace")
    if "route not found" in text or "no Route matched" in text:
        return "маршрута нет (но фаервол пропустил)"
    if status in (429, 439, 403) and ("<html" in text.lower() or "<!DOCTYPE" in text):
        return "ЗАКРЫТ фаерволом"
    if status == 200:
        return f"200 ОТВЕТИЛ: {text[:120]!r}"
    return f"{status}: {text[:100]!r}"


def probe(item_id: str) -> None:
    for template in CANDIDATES:
        path = template.format(id=item_id)
        url = "https://www.avito.ru" + path
        request = urllib.request.Request(
            url, headers={"User-Agent": IPHONE_UA, "Accept": "application/json",
                          "X-Requested-With": "XMLHttpRequest"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                status, body = response.status, response.read(400)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(400)
            except Exception:
                body = b""
        except Exception as exc:
            print(f"  {path:<45} {type(exc).__name__}")
            continue
        print(f"  {path:<45} {status:>3}  {classify(status, body)}")


def show(fragment: str, bundle: str) -> None:
    """Куски JS вокруг адреса — там видно параметры вызова."""
    request = urllib.request.Request(
        bundle, headers={"User-Agent": IPHONE_UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    body = raw.decode("utf-8", "replace")
    print(f"бандл {len(body) // 1024} КБ, ищу «{fragment}»\n")
    for number, match in enumerate(re.finditer(re.escape(fragment), body), 1):
        start = max(0, match.start() - 400)
        piece = body[start:match.end() + 500]
        print(f"--- вхождение {number}\n{piece}\n")
        if number >= 4:
            break


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", default="8245305594")
    parser.add_argument("--show", default="", help="показать куски JS вокруг строки")
    parser.add_argument("--bundle", default=BUNDLE)
    args = parser.parse_args()

    if args.show:
        show(args.show, args.bundle)
        return
    print("проверяю найденные маршруты:\n")
    probe(args.id)


if __name__ == "__main__":
    main()
