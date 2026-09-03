#!/usr/bin/env python3
"""
Сбор объявлений через каталог Avito — по 50 штук за запрос.

Тот же самый эндпоинт, которым сайт рисует выдачу поиска:

    GET /web/1/js/items?categoryId=..&locationId=..&page=N&limit=50

Он отдаёт готовый JSON, где уже есть всё, что нужно для CSV: заголовок,
описание, цена числом, фотографии, категория, город и адрес. Разбирать
HTML не приходится вовсе.

Разница с обходом страниц объявлений принципиальная: 30000 карточек — это
примерно 600 запросов вместо 30000, и мегабайты вместо десятков гигабайт.
Проверки фаервола (капча и proof-of-work) проходятся один раз на сессию,
дальше запросы идут свободно; когда сервер снова попросит — unlock.py
разберётся сам.

Avito не отдаёт выдачу глубже сотни страниц, поэтому 30000 не набрать
одним запросом: перебираем категории и города. Категории берутся из их
же справочника, а не выдумываются.

    RUCAPTCHA_API_KEY=... python catalog_scrape.py --limit 30000
    python catalog_scrape.py --limit 500 --out data/proba.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path

import requests

from scraper import CSV_FIELDS, DATA_DIR, Listing
from unlock import ensure_access

BASE = "https://www.avito.ru"
CATEGORY_TREE = BASE + "/web/1/category/tree"

# Крупные города: одной Москвы на 30000 не хватит, а глубже сотой
# страницы Avito выдачу не отдаёт.
LOCATIONS = [
    (637640, "Москва"), (653240, "Санкт-Петербург"), (641780, "Новосибирск"),
    (642780, "Екатеринбург"), (643580, "Казань"), (639140, "Нижний Новгород"),
    (645700, "Челябинск"), (656620, "Самара"), (652000, "Ростов-на-Дону"),
    (654240, "Уфа"), (628260, "Красноярск"), (649140, "Пермь"),
    (656060, "Воронеж"), (655260, "Волгоград"), (629380, "Краснодар"),
]

PROGRESS = DATA_DIR / "catalog_done.txt"     # какие объявления уже записаны


def size_of(name: str) -> int:
    """"636x636" -> 404496. Нужен самый большой размер картинки."""
    match = re.match(r"(\d+)x(\d+)$", name)
    return int(match.group(1)) * int(match.group(2)) if match else 0


def best_image(item: dict) -> str:
    images = item.get("images")
    if not isinstance(images, list) or not images:
        return ""
    first = images[0]
    if not isinstance(first, dict) or not first:
        return ""
    return max(first.items(), key=lambda pair: size_of(pair[0]))[1]


def to_listing(item: dict, item_id: int) -> Listing:
    """Объявление из каталога -> строка CSV."""
    # ?context=... — метка перехода, к самому объявлению отношения не
    # имеет и в ссылке не нужна
    url = BASE + str(item.get("urlPath", "")).split("?")[0]

    content = str(item.get("description") or "").strip()
    description = (content[:100] + "…") if len(content) > 100 else content

    price = (item.get("priceDetailed") or {}).get("value")
    geo = item.get("geo") or {}
    address = (geo.get("formattedAddress")
               or (item.get("addressDetailed") or {}).get("locationName", ""))

    return Listing(
        id=item_id,
        url=url,
        title=str(item.get("title") or "").strip(),
        content=content,
        description=description,
        image=best_image(item),
        price="" if price in (None, "") else str(price),
        category=str((item.get("category") or {}).get("name") or ""),
        city=str((item.get("location") or {}).get("name") or ""),
        address=str(address or ""),
    )


def categories(session, verbose: bool = True) -> list:
    """Разделы из справочника Avito, а не из головы."""
    response = ensure_access(session, CATEGORY_TREE, verbose=verbose)
    if response.status_code != 200:
        if verbose:
            print(f"справочник категорий не открылся ({response.status_code}), "
                  f"беру автомобили")
        return [(9, "Автомобили")]
    try:
        tree = response.json()
    except Exception:
        return [(9, "Автомобили")]

    found: list = []

    def walk(node) -> None:
        if isinstance(node, dict):
            node_id, name = node.get("id"), node.get("name")
            if isinstance(node_id, int) and isinstance(name, str):
                found.append((node_id, name))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree)
    unique = list(dict.fromkeys(found))
    if verbose:
        print(f"категорий в справочнике: {len(unique)}")
    return unique or [(9, "Автомобили")]


def load_done() -> set:
    if not PROGRESS.exists():
        return set()
    return {line.strip() for line in
            PROGRESS.read_text(encoding="utf-8").splitlines() if line.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=30000)
    parser.add_argument("--out", default=str(DATA_DIR / "avito.csv"))
    parser.add_argument("--pages", type=int, default=100,
                        help="сколько страниц брать из одной выдачи")
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--categories", default="",
                        help="через запятую, если справочник не нужен")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    if args.categories:
        sections = [(int(c), f"категория {c}")
                    for c in args.categories.split(",") if c.strip()]
    else:
        sections = categories(session)

    done = load_done()
    out_path = Path(args.out)
    next_id = len(done) + 1
    print(f"уже собрано ранее: {len(done)}; цель: {args.limit}\n")

    started = time.time()
    written = requests_made = 0
    with out_path.open("a", newline="", encoding="utf-8") as handle, \
            PROGRESS.open("a", encoding="utf-8") as progress:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if out_path.stat().st_size == 0:
            writer.writeheader()

        for location_id, city in LOCATIONS:
            for section_id, section in sections:
                if written + len(done) >= args.limit:
                    break
                for page in range(1, args.pages + 1):
                    if written + len(done) >= args.limit:
                        break
                    url = (f"{BASE}/web/1/js/items?categoryId={section_id}"
                           f"&locationId={location_id}&page={page}"
                           f"&limit={args.per_page}&display=list")
                    response = ensure_access(session, url, verbose=False)
                    requests_made += 1
                    if response.status_code != 200:
                        print(f"  {city}/{section}: страница {page} -> "
                              f"{response.status_code}, дальше не иду")
                        break
                    try:
                        items = response.json().get("items") or []
                    except Exception:
                        break

                    fresh = 0
                    for item in items:
                        # 51-й элемент выдачи — не объявление, а служебная
                        # запись без urlPath; такие пропускаем
                        if not isinstance(item, dict) or not item.get("urlPath"):
                            continue
                        key = str(item.get("id"))
                        if key in done:
                            continue
                        done.add(key)
                        writer.writerow(to_listing(item, next_id).__dict__)
                        progress.write(key + "\n")
                        next_id += 1
                        written += 1
                        fresh += 1
                    handle.flush()
                    progress.flush()

                    print(f"  {city}/{section}: страница {page} -> "
                          f"+{fresh} (всего {written + len(done) - fresh})")
                    if not fresh:
                        break        # выдача кончилась или пошли повторы
                    time.sleep(args.pause)

    elapsed = time.time() - started
    print(f"\nЗаписано за прогон: {written}, запросов: {requests_made}, "
          f"за {elapsed / 60:.1f} мин")
    print(f"CSV: {out_path}")


if __name__ == "__main__":
    main()
