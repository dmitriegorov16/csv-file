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
from unlock import ensure_access

BASE = "https://www.avito.ru"
OUT = DATA_DIR / "categories.json"


def probe(session, category_id: int, location_id: int, timeout: int = 30):
    """Одно объявление из раздела: есть ли он и как называется."""
    url = (f"{BASE}/web/1/js/items?categoryId={category_id}"
           f"&locationId={location_id}&page=1&limit=1&display=list")
    response = ensure_access(session, url, verbose=False, timeout=timeout)
    if response.status_code != 200:
        return None, response.status_code
    try:
        items = response.json().get("items") or []
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
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    working = []

    for category_id in range(args.start, args.end + 1):
        name, status = probe(session, category_id, args.location)
        if name:
            working.append({"id": category_id, "name": name})
            print(f"  {category_id:>4}  {name}")
        elif status != 200:
            print(f"  {category_id:>4}  — ответ {status}")
        time.sleep(args.pause)

    OUT.write_text(json.dumps(working, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nрабочих разделов: {len(working)} -> {OUT}")
    if working:
        print("сборщик подхватит этот файл сам")


if __name__ == "__main__":
    main()
