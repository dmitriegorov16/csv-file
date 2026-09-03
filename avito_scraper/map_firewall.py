#!/usr/bin/env python3
"""
Карта того, что фаервол Avito охраняет, а что нет.

Известный факт: страница блокировки сама ходит на
/web/5/firewallCaptcha/get, и этот запрос проходит. Значит защита
навешана не на весь /web/, а на отдельные префиксы. Мы уткнулись в
/web/1/items/ — но это одно семейство из многих.

Ответ сам себя объясняет, гадать не нужно:

    JSON "route not found"   префикс ОТКРЫТ, просто маршрут не тот
    HTML 429/439             префикс ЗАКРЫТ
    JSON с данными           нашли

Проверяются ещё и хосты: голый avito.ru без www и m.avito.ru — защита
иногда навешана на один хост, а не на все.

    python map_firewall.py
    python map_firewall.py --id 8245305594
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

# Имена, под которыми у Avito может лежать карточка объявления.
NAMES = ["items", "item", "iva", "itemView", "catalog", "serp", "search",
         "similar", "contacts", "delivery", "geo", "user", "profile",
         "autoteka", "favorites", "bx"]

# Хосты: страница объявления живёт на www, приложение на m, а голый домен
# иногда настроен отдельно.
PAGE_HOSTS = ["https://avito.ru", "https://m.avito.ru", "https://www.avito.ru"]


def classify(status: int, body: bytes) -> str:
    text = body[:400].decode("utf-8", "replace")
    if "route not found" in text or "no Route matched" in text:
        return "ОТКРЫТ (маршрута нет)"
    if "<!DOCTYPE" in text or "<html" in text.lower():
        if status in (429, 439, 403):
            return "ЗАКРЫТ фаерволом"
        return f"HTML {status}"
    if status == 200:
        return "ДАННЫЕ"
    return f"{status} {text[:60]!r}"


def request(url: str, timeout: int = 20):
    req = urllib.request.Request(
        url, headers={"User-Agent": IPHONE_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(400)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(400)
        except Exception:
            return exc.code, b""
    except Exception as exc:
        return 0, type(exc).__name__.encode()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", default="8245305594")
    parser.add_argument("--path", default="/arhangelskaya/akvarium/"
                                          "prodam_akvarium_s_rybkami_8245305594")
    args = parser.parse_args()

    print("контроль — этот маршрут заведомо открыт (его зовёт сама капча):")
    status, body = request("https://www.avito.ru/web/5/firewallCaptcha/get")
    print(f"  /web/5/firewallCaptcha/get   {status}  {classify(status, body)}\n")

    print("версии /web/N/items/:")
    for version in range(1, 10):
        url = f"https://www.avito.ru/web/{version}/items/{args.id}"
        status, body = request(url)
        print(f"  /web/{version}/items/{{id}}          {status:>3}  "
              f"{classify(status, body)}")

    print("\nимена под /web/1/ и /web/2/:")
    for version in (1, 2):
        for name in NAMES:
            url = f"https://www.avito.ru/web/{version}/{name}/{args.id}"
            status, body = request(url)
            verdict = classify(status, body)
            # открытые префиксы — единственное, ради чего всё затевалось
            mark = "  <<<" if verdict.startswith("ОТКРЫТ") or verdict == "ДАННЫЕ" else ""
            print(f"  /web/{version}/{name:<12}/{{id}}  {status:>3}  {verdict}{mark}")

    print("\nстраница объявления с разных хостов:")
    for host in PAGE_HOSTS:
        status, body = request(host + args.path)
        print(f"  {host:<24} {status:>3}  {classify(status, body)}")


if __name__ == "__main__":
    main()
