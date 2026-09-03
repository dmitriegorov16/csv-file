#!/usr/bin/env python3
"""
Проверка мобильного API Avito с ключом, подсмотренным в трафике.

Откуда ключ: при работе браузера в мобильном режиме страница сама
дёрнула m.avito.ru/web/1/network/check?key=af0deccbgcgidddjgnvljitntccddu...
Это ключ, которым мобильный клиент Avito подписывает свои запросы. Без
него те же пути отвечали 404, поэтому проверить с ним обязательно.

Если API отдаёт карточку в JSON — это лучший из возможных исходов:
никакого HTML и рендера, ответ в разы легче страницы, и структура
стабильная. Тогда весь сбор сводится к одному запросу на объявление.

Прокси и ротация сессии берутся из тех же переменных окружения, что и в
fast_scrape.py.

    python test_mobile_api.py
    python test_mobile_api.py 8329727762
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ключ из перехваченного запроса самой страницы
KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"

DEFAULT_ITEM = "8329727762"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def candidates(item_id: str) -> list:
    """Пути, которыми мобильный клиент может забирать карточку.

    Версия API в пути (число после /api/) у Avito менялась много раз,
    поэтому перебираем диапазон — лишний запрос дешевле, чем гадание."""
    urls = []
    for version in (1, 9, 11, 13, 14, 15, 16, 18, 20, 25):
        urls.append(f"https://m.avito.ru/api/{version}/items/{item_id}?key={KEY}")
    for version in (1, 2, 3):
        urls.append(f"https://m.avito.ru/web/{version}/items/{item_id}?key={KEY}")
        urls.append(f"https://www.avito.ru/web/{version}/items/{item_id}?key={KEY}")
    # варианты с явным указанием источника, как это делает приложение
    urls.append(f"https://m.avito.ru/api/16/items/{item_id}?key={KEY}&source=mobile_site")
    urls.append(f"https://m.avito.ru/api/1/item/{item_id}?key={KEY}")
    return urls


def proxy_opener():
    server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    if not server:
        print("(без прокси — напрямую с этой машины)\n")
        return urllib.request.build_opener()
    user = os.environ.get("AVITO_PROXY_USERNAME", "").strip()
    password = os.environ.get("AVITO_PROXY_PASSWORD", "").strip()
    if user:
        scheme, rest = server.split("://", 1)
        server = (f"{scheme}://{urllib.parse.quote(user, safe='')}:"
                  f"{urllib.parse.quote(password, safe='')}@{rest}")
    print(f"(через прокси {server.split('@')[-1]})\n")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": server, "https": server}))


def probe(opener, url: str, timeout: int = 25):
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(600).decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, body
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> None:
    item_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ITEM
    opener = proxy_opener()
    print(f"Ключ: {KEY[:20]}...  объявление: {item_id}")
    print("404 = такого пути нет | 403/429/439 = путь есть, но не пускают | "
          "200 = данные\n")

    winners = []
    for url in candidates(item_id):
        status, body = probe(opener, url)
        short = url.replace("https://", "").split("?")[0]
        print(f"  {str(status):>5}  {short}")
        if status == 200:
            winners.append((url, body))
            preview = " ".join(body[:200].split())
            print(f"         {preview}")

    if not winners:
        print("\nНи один путь не отдал данные.")
        print("Если везде 404 — ключ к этим путям отношения не имеет.")
        print("Если 403/429 — пути живые, дело в блокировке, а не в ключе.")
        return

    url, body = winners[0]
    with open("api_item.json", "w", encoding="utf-8") as handle:
        handle.write(body)
    print(f"\nРАБОТАЕТ: {url}")
    print("Ответ сохранён в api_item.json")
    try:
        data = json.loads(body)
        print("Ключи верхнего уровня:", list(data)[:20])
    except Exception:
        print("(ответ не разобрался как JSON — посмотрим глазами)")


if __name__ == "__main__":
    main()
