#!/usr/bin/env python3
"""
Капча по JSON, без страницы-заглушки.

Заблокированный адрес отвечает не пустым отказом, а указанием, что
делать: "link": "ru.avito://1/firewall/captcha/show". То есть проверка
проходима, просто через капчу, а не через proof-of-work — какой режим
дадут, зависит от репутации IP.

Раньше мы решали капчу, разбирая HTML страницы блокировки. Здесь HTML
нет вовсе, поэтому идём сразу в те же два адреса, что зовёт их скрипт:

    POST /web/5/firewallCaptcha/get      -> какой виджет показать
    POST /web/3/firewallCaptcha/verify   -> {"verified": true}

Ключи виджетов взяты из сохранённой страницы 429, поэтому подставлять
их наугад не приходится.

    RUCAPTCHA_API_KEY=... python try_captcha.py
    RUCAPTCHA_API_KEY=... python try_captcha.py --proxy http://1.2.3.4:8080
"""

from __future__ import annotations

import argparse
import json
import os

import requests

import firewall

CATALOG = ("https://www.avito.ru/web/1/js/items"
           "?categoryId=9&locationId=637640&page=1&limit=50&display=list")

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Ключи со страницы 429 — на случай, если решать придётся без неё.
KNOWN_KEYS = ('captchaId: "2d9c743cf7d63dbc9db578a608196bcd" '
              'data-sitekey="070db171-ddb9-4c93-b7f6-d25d3c9d7e28"')


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=CATALOG)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--save", default="data/catalog.json")
    parser.add_argument("--quiet", action="store_true",
                        help="не печатать, что уходит на verify")
    args = parser.parse_args()

    if not os.environ.get("RUCAPTCHA_API_KEY"):
        raise SystemExit("Нужен ключ: RUCAPTCHA_API_KEY=... python try_captcha.py")

    session = requests.Session()
    if args.proxy:
        proxy = args.proxy if "://" in args.proxy else "http://" + args.proxy
        session.proxies.update({"http": proxy, "https": proxy})
    headers = {"User-Agent": DESKTOP_UA, "Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest",
               "Referer": "https://www.avito.ru/moskva/avtomobili"}

    print(f"1) запрос: {args.url[:88]}")
    response = session.get(args.url, headers=headers, timeout=30)
    print(f"   {response.status_code}, {len(response.content)} б: "
          f"{response.text[:120]}")

    if response.status_code != 200:
        print("\n2) что предлагает фаервол")
        probe = session.post("https://www.avito.ru/web/5/firewallCaptcha/get",
                             json={"refreshInternalCaptcha": False},
                             headers={**headers, "Content-Type": "application/json"},
                             timeout=30)
        print(f"   /get -> {probe.status_code}: {probe.text[:300]}")

        print("\n3) решаю")
        solved, note = firewall.solve(session, args.url, KNOWN_KEYS,
                                      debug=not args.quiet)
        print(f"   {note}")
        if not solved:
            raise SystemExit("\nне прошли")

        print("\n4) повторяю запрос")
        response = session.get(args.url, headers=headers, timeout=30)
        print(f"   {response.status_code}, {len(response.content)} байт")

    if response.status_code != 200:
        raise SystemExit(f"после проверки всё равно {response.status_code}: "
                         f"{response.text[:200]}")

    data = response.json()
    with open(args.save, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    items = firewall.find_type and data.get("items")
    print(f"\nсохранено в {args.save}")
    print(f"ключи ответа: {', '.join(list(data)[:12])}")
    if isinstance(items, list):
        print(f"объявлений: {len(items)}")


if __name__ == "__main__":
    main()
