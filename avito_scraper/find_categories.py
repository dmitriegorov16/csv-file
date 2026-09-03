#!/usr/bin/env python3
"""
Какие categoryId поиск действительно понимает.

Справочник /web/1/category/tree отдаёт свою нумерацию: 600 запросов по
его идентификаторам вернули ноль объявлений, тогда как categoryId=9
работает и даёт полсотни. Значит числа там другие, и подставлять их в
поиск бессмысленно.

Поэтому не гадаем, а спрашиваем сам поиск: перебираем номера, берём по
одному объявлению и смотрим, что вернулось. Имя раздела приходит внутри
объявления (category.name), так что список получается не выдуманный, а
проверенный — с ним сборщик точно не будет крутить пустые страницы.

    RUCAPTCHA_API_KEY=... python find_categories.py
    python find_categories.py --to 200
"""

from __future__ import annotations

import argparse
import json
import time

import requests

from scraper import DATA_DIR
from unlock import ensure_access, find_items, open_session

BASE = "https://www.avito.ru"
OUT = DATA_DIR / "categories.json"


def probe(session, category_id: int, location_id: int, timeout: int = 30,
          verbose: bool = False):
    """Одно объявление из раздела: есть ли он и как называется."""
    # limit=1 не годится: на такой запрос выдача приходит пустой, и все
    # разделы выглядят несуществующими — при полностью живом доступе.
    # Десятка достаточно, чтобы среди ответа нашлось настоящее объявление
    # (первым может стоять служебная запись без urlPath).
    url = (f"{BASE}/web/1/js/items?categoryId={category_id}"
           f"&locationId={location_id}&page=1&limit=10&display=list")
    response = ensure_access(session, url, verbose=verbose, timeout=timeout)
    if response.status_code != 200:
        return None, response.status_code
    try:
        items = find_items(response.json())
    except Exception:
        return None, "не JSON"
    for item in items:
        if isinstance(item, dict) and item.get("urlPath"):
            name = (item.get("category") or {}).get("name") or ""
            return name, 200
    return "", 200


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", type=int, default=1)
    parser.add_argument("--to", dest="end", type=int, default=120)
    parser.add_argument("--location", type=int, default=637640)
    parser.add_argument("--pause", type=float, default=0.3)
    parser.add_argument("--proxy", default="",
                        help="ходить через этот адрес: репутация IP решает, "
                             "в каком режиме с нами разговаривают")
    parser.add_argument("--proxy-list-url", default="",
                        help="взять случайный адрес из списка провайдера")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Сначала открываем доступ и только потом перебираем номера:
    # проверять сотню разделов, не зная, пускают ли нас вообще,
    # бессмысленно — все ответы будут одинаково пустыми.
    print("разогрев: ищу адрес, через который Avito отвечает")
    session, _ = open_session(args.proxy, args.proxy_list_url)
    if session is None:
        print("Доступ не открылся ни через один адрес. Перебор ничего не даст.")
        print("Подождите 10–15 минут и повторите — или запустите без прокси.")
        return

    working = []

    refused = 0
    for category_id in range(args.start, args.end + 1):
        name, status = probe(session, category_id, args.location)
        if name:
            working.append({"id": category_id, "name": name})
            print(f"  {category_id:>4}  {name}")
            refused = 0
        elif status != 200:
            print(f"  {category_id:>4}  — ответ {status}")
            refused += 1
            # Подряд идущие отказы значат, что нас снова закрыли, а не что
            # разделов нет. Дальше перебирать — впустую жечь запросы и
            # злить фаервол.
            if refused >= 5:
                print("\nПять отказов подряд — доступ снова закрыт. "
                      "Останавливаюсь.")
                break
        else:
            refused = 0
        time.sleep(args.pause)

    OUT.write_text(json.dumps(working, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nрабочих разделов: {len(working)} -> {OUT}")
    if working:
        print("сборщик подхватит этот файл сам")


if __name__ == "__main__":
    main()
