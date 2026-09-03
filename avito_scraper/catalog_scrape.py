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
import sys
import time
from pathlib import Path

import requests

import notify
from scraper import CSV_FIELDS, DATA_DIR, Listing
from unlock import ensure_access, find_items, open_session
from url_meta import city_from_url

BASE = "https://www.avito.ru"

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
    detailed = item.get("addressDetailed") or {}

    # Город: location.name есть не у всех объявлений, поэтому спускаемся
    # по запасным источникам, а в самом конце берём из ссылки — там он
    # присутствует всегда, потому что задан структурой адреса Avito.
    city = (str((item.get("location") or {}).get("name") or "")
            or str(detailed.get("locationName") or "")
            or str(geo.get("addressLocality") or "")
            or city_from_url(url))

    # Адрес — только настоящий. Раньше сюда при его отсутствии падало
    # название города, и колонка дублировала соседнюю, изображая данные,
    # которых нет. Пусто честнее.
    address = str(geo.get("formattedAddress") or "").strip()
    if address == city:
        address = ""

    return Listing(
        id=item_id,
        url=url,
        title=str(item.get("title") or "").strip(),
        content=content,
        description=description,
        image=best_image(item),
        price="" if price in (None, "") else str(price),
        category=str((item.get("category") or {}).get("name") or ""),
        city=city,
        address=address,
    )


CATEGORIES_FILE = DATA_DIR / "categories.json"


def categories(session, verbose: bool = True) -> list:
    """Разделы для обхода.

    Берём проверенный список: номера, на которые поиск действительно
    отвечает объявлениями. Справочник /category/tree на эту роль не
    годится, хотя и выглядит подходящим: у него своя нумерация, и обход
    по ней даёт ровно ноль объявлений на каждой странице — при этом лог
    выглядит как работа, что хуже честной ошибки."""
    if CATEGORIES_FILE.exists():
        try:
            saved = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
            pairs = [(int(row["id"]), row["name"]) for row in saved if row.get("id")]
            if pairs:
                if verbose:
                    print(f"разделов из {CATEGORIES_FILE.name}: {len(pairs)}")
                return pairs
        except Exception:
            pass

    # Проверенного списка нет — находим его сами. Справочник
    # /category/tree сюда не годится: его номера поиск не понимает, и
    # обход по ним даёт нули в каждой строке лога, притворяясь работой.
    if verbose:
        print("проверенного списка разделов нет — ищу рабочие номера "
              "(это займёт пару минут, зато потом сохранится)")
    from find_categories import probe

    found = []
    refused = 0
    for category_id in range(1, 121):
        name, code = probe(session, category_id, LOCATIONS[0][0])
        if name:
            found.append({"id": category_id, "name": name})
            if verbose:
                print(f"   {category_id:>4}  {name}")
            refused = 0
        elif code != 200:
            refused += 1
            if refused >= 5:
                if verbose:
                    print("   пять отказов подряд — доступ закрылся, "
                          "останавливаю поиск")
                break
        time.sleep(0.3)

    if found:
        CATEGORIES_FILE.write_text(
            json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
        if verbose:
            print(f"нашёл разделов: {len(found)}, сохранил в "
                  f"{CATEGORIES_FILE.name}\n")
        return [(row["id"], row["name"]) for row in found]

    raise SystemExit(
        "Не нашлось ни одного рабочего раздела. Собирать нечего: обход по "
        "выдуманным номерам даёт нули. Попробуйте позже или через другой адрес.")


def send_chunk(rows: list, total: int) -> str:
    """Отправить порцию строк отдельным CSV-файлом.

    Именно файлом, а не текстом: строку объявления с описанием в чат не
    впихнуть, а CSV открывается на телефоне и сразу видно, что собралось."""
    chunks_dir = DATA_DIR / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    path = chunks_dir / f"{rows[0].id:06d}-{rows[-1].id:06d}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    return notify.send_file(
        path, f"Строки {rows[0].id}–{rows[-1].id}, всего собрано {total}")


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
    parser.add_argument("--max-categories", type=int, default=40,
                        help="сколько разделов брать из справочника")
    parser.add_argument("--chunk", type=int, default=50,
                        help="каждые столько строк отправлять порцию в Telegram")
    parser.add_argument("--telegram", action="store_true",
                        help="слать порции и итоговый файл в Telegram")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-list-url", default="",
                        help="взять случайный адрес из списка провайдера")
    args = parser.parse_args()

    # то же самое для сбора: он идёт часами в фоне, и его лог должен
    # писаться по мере работы, а не выгружаться в конце
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session, _ = open_session(args.proxy, args.proxy_list_url)
    if session is None:
        raise SystemExit("Доступ к Avito не открылся ни через один адрес. "
                         "Подождите и повторите.")

    if args.categories:
        sections = [(int(c), f"категория {c}")
                    for c in args.categories.split(",") if c.strip()]
    else:
        sections = categories(session)
        if len(sections) > args.max_categories:
            print(f"беру первые {args.max_categories} из {len(sections)}")
            sections = sections[:args.max_categories]

    if args.telegram:
        if not notify.enabled():
            print("TELEGRAM_TOKEN не задан — отправлять нечем\n")
        else:
            hello = (f"Начинаю сбор: цель {args.limit}, разделов "
                     f"{len(sections)}, городов {len(LOCATIONS)}")
            print(f"telegram: {notify.send_message(hello)}\n")

    done = load_done()
    out_path = Path(args.out)
    next_id = len(done) + 1
    print(f"уже собрано ранее: {len(done)}; цель: {args.limit}\n")

    started = time.time()
    written = requests_made = refused = 0
    pending: list = []          # строки, ещё не ушедшие порцией в Telegram
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
                        refused += 1
                        # Адрес мог умереть посреди прогона: прокси живут
                        # недолго. Несколько отказов подряд значат, что
                        # дело в адресе, а не в разделе, — берём новый и
                        # проходим проверки заново.
                        if refused >= 3:
                            print("      три отказа подряд — меняю адрес")
                            fresh_session, _ = open_session(
                                args.proxy, args.proxy_list_url, verbose=True)
                            if fresh_session is None:
                                print("      новый адрес не открылся, "
                                      "останавливаюсь")
                                raise SystemExit(1)
                            session = fresh_session
                            refused = 0
                        break
                    refused = 0
                    try:
                        items = find_items(response.json())
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
                        listing = to_listing(item, next_id)
                        writer.writerow(listing.__dict__)
                        pending.append(listing)
                        progress.write(key + "\n")
                        next_id += 1
                        written += 1
                        fresh += 1
                    handle.flush()
                    progress.flush()

                    # сколько пришло и сколько записалось — разные числа:
                    # ноль пришедших значит негодный раздел, ноль
                    # записанных при непустом ответе — что всё это уже есть
                    print(f"  {city}/{section}: страница {page} -> "
                          f"пришло {len(items)}, новых {fresh} "
                          f"(всего {written + len(done) - fresh})")

                    # Порция уходит файлом по мере накопления: так видно,
                    # что собирается именно нужное, уже на пятидесятой
                    # строке, а не на тридцатитысячной.
                    while args.chunk and len(pending) >= args.chunk:
                        batch, pending = pending[:args.chunk], pending[args.chunk:]
                        chunk_path = send_chunk(batch, len(done))
                        print(f"      порция {len(batch)} строк -> "
                              f"{chunk_path}")

                    if not fresh:
                        break        # выдача кончилась или пошли повторы
                    time.sleep(args.pause)

    elapsed = time.time() - started
    summary = (f"Записано за прогон: {written}, запросов: {requests_made}, "
               f"за {elapsed / 60:.1f} мин. Всего в файле: {len(done)}")
    print(f"\n{summary}")
    print(f"CSV: {out_path}")

    if args.chunk and pending:
        print(f"      остаток {len(pending)} строк -> "
              f"{send_chunk(pending, len(done))}")

    if args.telegram and notify.enabled():
        notify.send_message(summary)
        # Итоговый файл целиком — ради него всё и затевалось.
        print(f"telegram: весь CSV -> "
              f"{notify.send_file(out_path, f'Итог: {len(done)} объявлений')}")


if __name__ == "__main__":
    main()
