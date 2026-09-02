#!/usr/bin/env python3
"""
Разведка прямых HTTP-путей к данным объявления — без браузера.

Проверяет две гипотезы разом:

1) TLS-отпечаток. Обычный curl/urllib получает от фаервола Avito 439, а
   настоящий Chrome с того же IP — решаемую капчу. Значит фаервол смотрит
   не только на IP, но и на то, похож ли клиент на браузер (JA3-отпечаток
   TLS, порядок заголовков, HTTP/2). Библиотека curl_cffi умеет слать
   запросы с отпечатком реального Chrome, оставаясь обычным HTTP-клиентом.
   Ставится так:  pip install curl_cffi

2) Внутреннее API. В трафике страницы видны эндпоинты вида
   www.avito.ru/web/1/delivery/conditions/<item_id>/buyer — то есть у
   Avito есть JSON-API под /web/N/. Если оттуда достаётся карточка, это
   лучше HTML во всех отношениях: меньше трафика, никакого рендера.

Запуск (прокси подхватывается из тех же переменных, что и scraper.py):

    python probe_endpoints.py
    python probe_endpoints.py 8329727762
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ITEM_ID = "8329727762"
DEFAULT_ITEM_URL = ("https://www.avito.ru/sankt-peterburg/avtomobili/"
                    "hyundai_solaris_1.6_at_2019_395_000_km_8329727762")

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def candidate_endpoints(item_id: str) -> list[str]:
    """Кандидаты на JSON-эндпоинт карточки. Пространство /web/N/ —
    подсмотрено в реальном трафике страницы, остальное — вариации."""
    return [
        # известно, что существует (видели в трафике) — проверка самого
        # факта, открыт ли /web/ для прямых запросов
        f"https://www.avito.ru/web/1/delivery/conditions/{item_id}/buyer",
        f"https://www.avito.ru/web/1/network/check",
        # вероятные формы эндпоинта карточки
        f"https://www.avito.ru/web/1/item/{item_id}",
        f"https://www.avito.ru/web/2/item/{item_id}",
        f"https://www.avito.ru/web/3/item/{item_id}",
        f"https://www.avito.ru/web/1/items/{item_id}",
        f"https://www.avito.ru/web/1/item/{item_id}/card",
        f"https://www.avito.ru/web/1/item/{item_id}/item-card",
        f"https://www.avito.ru/web/1/items/{item_id}/card",
        # старые/мобильные формы
        f"https://www.avito.ru/api/1/items/{item_id}",
        f"https://www.avito.ru/api/11/items/{item_id}",
        f"https://www.avito.ru/js/1/item/{item_id}",
        f"https://m.avito.ru/api/13/items/{item_id}",
    ]


def proxy_opener():
    server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    if not server:
        return urllib.request.build_opener()
    user = os.environ.get("AVITO_PROXY_USERNAME", "").strip()
    password = os.environ.get("AVITO_PROXY_PASSWORD", "").strip()
    if user:
        scheme, rest = server.split("://", 1)
        server = f"{scheme}://{user}:{password}@{rest}"
    print(f"[proxy] {server.split('@')[-1]}")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": server, "https": server}))


def probe_urllib(url: str, opener) -> tuple[str, str]:
    request = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with opener.open(request, timeout=25) as response:
            body = response.read(400)
            return str(response.status), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(400)
        except Exception:
            pass
        return str(exc.code), body.decode("utf-8", "replace")
    except Exception as exc:
        return type(exc).__name__, str(exc)[:200]


def summarize(body: str) -> str:
    body = " ".join(body.split())
    if "Доступ ограничен" in body or "js-firewall-form" in body:
        return "ФАЕРВОЛ"
    return (body[:110] + "…") if len(body) > 110 else (body or "(пусто)")


def main() -> None:
    item_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ITEM_ID
    opener = proxy_opener()

    print(f"\n=== 1. Внутреннее API (обычный HTTP-клиент), item_id={item_id}\n")
    for url in candidate_endpoints(item_id):
        status, body = probe_urllib(url, opener)
        print(f"  {status:>12}  {url.replace('https://', '')}")
        if status.isdigit() and status.startswith("2"):
            print(f"                 -> {summarize(body)}")

    print(f"\n=== 2. Страница объявления обычным HTTP-клиентом\n")
    status, body = probe_urllib(DEFAULT_ITEM_URL, opener)
    print(f"  {status:>12}  {summarize(body)}")

    print(f"\n=== 3. То же, но с TLS-отпечатком настоящего Chrome (curl_cffi)\n")
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("  curl_cffi не установлен — пропускаю.")
        print("  Поставить:  pip install curl_cffi")
        return

    proxies = None
    server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    if server:
        user = os.environ.get("AVITO_PROXY_USERNAME", "").strip()
        password = os.environ.get("AVITO_PROXY_PASSWORD", "").strip()
        if user:
            scheme, rest = server.split("://", 1)
            server = f"{scheme}://{user}:{password}@{rest}"
        proxies = {"http": server, "https": server}

    for impersonate in ("chrome124", "chrome120", "chrome110"):
        try:
            response = cffi_requests.get(DEFAULT_ITEM_URL, impersonate=impersonate,
                                         proxies=proxies, timeout=30)
        except Exception as exc:
            print(f"  {impersonate:>12}  ошибка: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        text = response.text
        marker = "ФАЕРВОЛ" if ("Доступ ограничен" in text or "js-firewall-form" in text) else "похоже на карточку"
        has_title = 'property="og:title"' in text
        print(f"  {impersonate:>12}  status={response.status_code}  {marker}  "
              f"og:title={'есть' if has_title else 'нет'}  ({len(text)} байт)")
        if has_title:
            start = text.find('property="og:title"')
            print(f"                 {text[max(0, start - 120):start + 80]}")
            with open("item_direct.html", "w", encoding="utf-8") as handle:
                handle.write(text)
            print("                 HTML сохранён: item_direct.html")
            break


if __name__ == "__main__":
    main()
