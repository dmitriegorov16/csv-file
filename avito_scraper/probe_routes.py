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
from pathlib import Path

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

# Путь к бандлу угадывать нельзя: в имени хеш сборки, и он меняется с
# каждым релизом. Берём ссылки из сохранённой страницы — там они точные.
SCRIPT_RE = re.compile(r'src=["\'](https://[^"\']+\.js)["\']', re.IGNORECASE)

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


def download(url: str, timeout: int = 120) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": IPHONE_UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def show(fragment: str, page_path: str, per_file: int = 3) -> None:
    """Куски JS вокруг адреса — там видно метод и параметры вызова."""
    page = Path(page_path).read_text(encoding="utf-8", errors="replace")
    scripts = list(dict.fromkeys(SCRIPT_RE.findall(page)))
    if not scripts:
        raise SystemExit(f"В {page_path} нет ссылок на JS")

    print(f"скриптов: {len(scripts)}, ищу «{fragment}»\n")
    total = 0
    for url in scripts:
        name = url.rsplit("/", 1)[-1]
        try:
            body = download(url)
        except Exception as exc:
            continue
        matches = list(re.finditer(re.escape(fragment), body))
        if not matches:
            continue
        print(f"=== {name}: {len(matches)} вхождений")
        for number, match in enumerate(matches[:per_file], 1):
            start = max(0, match.start() - 400)
            print(f"--- {number}\n{body[start:match.end() + 500]}\n")
        total += len(matches)
    if not total:
        print("нигде не встретилось")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", default="8245305594")
    parser.add_argument("--show", default="", help="показать куски JS вокруг строки")
    parser.add_argument("--page", default="data/empty_200.html",
                        help="сохранённая страница — из неё берутся ссылки на JS")
    args = parser.parse_args()

    if args.show:
        show(args.show, args.page)
        return
    print("проверяю найденные маршруты:\n")
    probe(args.id)


if __name__ == "__main__":
    main()
