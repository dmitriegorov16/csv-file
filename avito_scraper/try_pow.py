#!/usr/bin/env python3
"""
Сквозная проверка: 439 -> решаем PoW -> получаем данные.

Проверяет всю цепочку целиком на живом Avito, с обычного IP, без прокси
и без капчи:

    запрос -> 439 с задачей -> /firewallPow/get -> подбор nonce
           -> /firewallPow/verify -> повтор запроса -> данные

Если последний шаг вернёт 200, значит платить за капчу и за мобильные
прокси больше не нужно: доступ покупается процессорным временем.

    python try_pow.py                      # каталог (пачка объявлений)
    python try_pow.py --url https://www.avito.ru/...   # обычная страница
"""

from __future__ import annotations

import argparse
import json
import time

import requests

import firewall_pow

CATALOG = ("https://www.avito.ru/web/1/js/items"
           "?categoryId=9&locationId=637640&page=1&limit=50&display=list")

# Задача приходит с кодом 439, а 403 — это ужесточённый режим, в котором
# сервер её даже не предлагает. Разница важная: 403 значит "подожди", а не
# "невозможно". Пробуем разные адреса и ждём между попытками.
VARIANTS = [
    CATALOG,
    "https://www.avito.ru/web/1/js/items?categoryId=9&locationId=637640&countOnly=1",
    "https://www.avito.ru/web/1/js/items?categoryId=98&page=1&limit=50",
    "https://www.avito.ru/web/1/long/js/items?categoryId=9&locationId=637640&page=1",
]

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="")
    parser.add_argument("--save", default="data/catalog.json")
    parser.add_argument("--attempts", type=int, default=12,
                        help="сколько раз попробовать поймать задачу")
    parser.add_argument("--pause", type=float, default=20.0,
                        help="пауза между попытками, секунд")
    args = parser.parse_args()

    session = requests.Session()
    headers = {"User-Agent": DESKTOP_UA, "Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest",
               "Referer": "https://www.avito.ru/moskva/avtomobili"}

    urls = [args.url] if args.url else VARIANTS

    print("1) ищу ответ с задачей (нужен 439 с pow_challenge)")
    response = None
    target = urls[0]
    for attempt in range(1, args.attempts + 1):
        target = urls[(attempt - 1) % len(urls)]
        response = session.get(target, headers=headers, timeout=30)
        has_task = "pow_challenge" in response.text
        print(f"   [{attempt}/{args.attempts}] {response.status_code} "
              f"{len(response.content):>6} б  "
              f"{'ЗАДАЧА ЕСТЬ' if has_task else response.text[:70]}")
        if response.status_code == 200 or has_task:
            break
        if attempt < args.attempts:
            time.sleep(args.pause)
    else:
        raise SystemExit("\nЗа все попытки задача не пришла — IP в жёстком "
                         "режиме. Это не тупик: подождите подольше и "
                         "повторите, 439 приходил с этого же адреса.")

    if response.status_code == 200:
        print("   пропустили сразу, PoW не понадобился")
    else:
        print("\n2) решаю proof-of-work")
        solved, note = firewall_pow.solve(session, target, response.text)
        print(f"   {note}")
        if not solved:
            raise SystemExit("\nне прошли — дальше идти незачем")

        print("\n3) повторяю запрос с той же сессией")
        response = session.get(target, headers=headers, timeout=30)
        print(f"   ответ {response.status_code}, {len(response.content)} байт")

    if response.status_code != 200:
        raise SystemExit(f"\nпосле проверки всё равно {response.status_code}: "
                         f"{response.text[:200]}")

    try:
        data = response.json()
    except Exception:
        raise SystemExit(f"\n200, но не JSON: {response.text[:200]}")

    with open(args.save, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    print(f"\nответ сохранён в {args.save}")

    items = firewall_pow._find(data, "items")
    if isinstance(items, list):
        print(f"объявлений в ответе: {len(items)}")
        if items:
            first = items[0]
            keys = ", ".join(list(first)[:14]) if isinstance(first, dict) else ""
            print(f"поля первого: {keys}")
    else:
        print(f"ключи ответа: {', '.join(list(data)[:12])}")


if __name__ == "__main__":
    main()
