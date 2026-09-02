#!/usr/bin/env python3
"""
Сбор карточек Avito из открытых веб-архивов — без обращения к Avito.

Идея: страницы Avito уже скачаны до нас и лежат в открытом доступе.
Common Crawl — открытый корпус обхода веба (терабайты HTML, обновляется
ежемесячно), Wayback Machine — веб-архив. Оба отдают сохранённые копии
страниц по своим API. Мы обращаемся к архиву, а не к Avito, поэтому ни
блокировок по IP, ни капчи, ни прокси здесь нет в принципе.

Как устроен Common Crawl:

  1. индекс по URL говорит, какие страницы есть в обходе и ГДЕ они лежат:
     имя WARC-файла, смещение и длина куска;
  2. этот кусок забирается Range-запросом и распаковывается — внутри
     лежит исходный HTML страницы ровно таким, каким его отдал сайт.

Ограничение, о котором надо помнить: данные архивные. Обход делается
раз в месяц-два, так что объявления будут той давности, а часть из них
уже снята с публикации. Для среза рынка это нормально, для "актуальных
объявлений на сегодня" — нет.

    python archive_scrape.py --check                  # что вообще есть в архиве
    python archive_scrape.py --category avtomobili --limit 100
    python archive_scrape.py --limit 30000            # все категории подряд
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict

from fast_scrape import (COUNTER_FILE, DONE_FILE, load_counter, load_done,
                         parse_html, save_counter)
from scraper import CSV_FIELDS, DATA_DIR, ITEM_LINK_RE, OUTPUT_CSV

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_DATA = "https://data.commoncrawl.org/"
WAYBACK_AVAILABLE = "http://archive.org/wayback/available?url="

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; avito-csv-scraper/1.0)"}


def http_get(url: str, timeout: int = 60, headers: dict = None) -> bytes:
    request = urllib.request.Request(url, headers={**HEADERS, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_crawls(count: int = 3) -> list:
    """Свежие обходы Common Crawl, новые первыми."""
    data = json.loads(http_get(CC_COLLINFO, timeout=45).decode("utf-8"))
    return [entry["id"] for entry in data[:count]]


def query_index(crawl: str, url_pattern: str, limit: int, page: int = 0) -> list:
    """Спрашивает индекс: какие страницы по шаблону есть в этом обходе.

    Возвращает записи с полями url / filename / offset / length — этого
    достаточно, чтобы потом вытащить сам HTML."""
    query = (f"https://index.commoncrawl.org/{crawl}-index"
             f"?url={urllib.request.quote(url_pattern, safe='')}"
             f"&output=json&limit={limit}&page={page}")
    try:
        raw = http_get(query, timeout=120).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise

    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("status") != "200":
            continue
        records.append(record)
    return records


def fetch_from_warc(record: dict, timeout: int = 60) -> str:
    """Достаёт исходный HTML страницы из архива по координатам из индекса."""
    offset = int(record["offset"])
    length = int(record["length"])
    url = CC_DATA + record["filename"]
    raw = http_get(url, timeout=timeout,
                   headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    payload = gzip.decompress(raw)
    # WARC-заголовки, затем HTTP-заголовки, затем тело — разделены пустой строкой
    parts = payload.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        return ""
    return parts[2].decode("utf-8", "replace")


def fetch_from_wayback(url: str, timeout: int = 45) -> str:
    """Запасной источник: копия страницы из Wayback Machine."""
    try:
        info = json.loads(http_get(WAYBACK_AVAILABLE + urllib.request.quote(url, safe=""),
                                   timeout=timeout).decode("utf-8"))
        snapshot = info.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return ""
        # id_ отдаёт исходный HTML без обвязки архива
        snapshot_url = snapshot["url"].replace("/http", "id_/http", 1)
        return http_get(snapshot_url, timeout=timeout).decode("utf-8", "replace")
    except Exception:
        return ""


def check_availability(category: str) -> None:
    """Разведка: есть ли вообще в архиве карточки Avito и сколько."""
    print("Спрашиваю Common Crawl, какие обходы доступны...")
    try:
        crawls = latest_crawls(3)
    except Exception as exc:
        sys.exit(f"Не достучался до Common Crawl: {type(exc).__name__}: {exc}")
    print(f"  свежие обходы: {', '.join(crawls)}\n")

    pattern = f"www.avito.ru/*/{category}/*" if category else "www.avito.ru/*"
    for crawl in crawls:
        print(f"{crawl}: ищу {pattern}")
        try:
            records = query_index(crawl, pattern, limit=200)
        except Exception as exc:
            print(f"  ошибка: {type(exc).__name__}: {exc}")
            continue
        items = [r for r in records if ITEM_LINK_RE.match(r["url"].split("?")[0])]
        print(f"  страниц в выборке: {len(records)}, из них карточек: {len(items)}")
        for record in items[:3]:
            print(f"    {record['url'][:95]}")
        if items:
            print(f"\n  Карточки в архиве есть. Пробую достать HTML первой...")
            try:
                page_html = fetch_from_warc(items[0])
                has_title = 'og:title' in page_html
                print(f"  получено {len(page_html)} байт, og:title "
                      f"{'НА МЕСТЕ' if has_title else 'отсутствует'}")
                if has_title:
                    listing = parse_html(page_html, items[0]["url"], 0)
                    print(f"    title: {listing.title[:70]}")
                    print(f"    цена:  {listing.price or '—'}")
                    print(f"    город: {listing.city or '—'}")
                    print("\nРАБОТАЕТ: данные достаются из архива, Avito не участвует.")
                    return
            except Exception as exc:
                print(f"  не удалось достать HTML: {type(exc).__name__}: {exc}")
    print("\nКарточек в проверенных обходах не нашлось — попробуйте другую "
          "категорию или запустите без --category.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="только разведка: есть ли карточки в архиве")
    parser.add_argument("--category", default="avtomobili",
                        help="категория Avito (пусто — все подряд)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--crawls", type=int, default=3,
                        help="сколько последних обходов перебрать")
    parser.add_argument("--pause", type=float, default=0.3)
    args = parser.parse_args()

    if args.check:
        check_availability(args.category)
        return

    crawls = latest_crawls(args.crawls)
    pattern = f"www.avito.ru/*/{args.category}/*" if args.category else "www.avito.ru/*"
    print(f"обходы: {', '.join(crawls)}\nшаблон: {pattern}\n")

    done = load_done()
    next_id = load_counter()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_CSV.exists()
    collected = skipped = failed = 0

    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as handle, \
            DONE_FILE.open("a", encoding="utf-8") as done_handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for crawl in crawls:
            page = 0
            while collected < args.limit:
                try:
                    records = query_index(crawl, pattern, limit=1000, page=page)
                except Exception as exc:
                    print(f"[{crawl}] индекс недоступен: {type(exc).__name__}")
                    break
                if not records:
                    break
                print(f"[{crawl}] страница индекса {page}: записей {len(records)}")
                page += 1

                for record in records:
                    if collected >= args.limit:
                        break
                    url = record["url"].split("?")[0]
                    if not ITEM_LINK_RE.match(url) or url in done:
                        skipped += 1
                        continue
                    try:
                        page_html = fetch_from_warc(record)
                    except Exception as exc:
                        failed += 1
                        print(f"  ошибка архива: {type(exc).__name__}")
                        continue

                    listing = parse_html(page_html, url, next_id)
                    done.add(url)
                    done_handle.write(url + "\n")
                    done_handle.flush()

                    if not listing.title:
                        failed += 1
                        continue

                    writer.writerow(asdict(listing))
                    handle.flush()
                    next_id += 1
                    collected += 1
                    save_counter(next_id)
                    print(f"  [{collected}/{args.limit}] {listing.title[:55]} | "
                          f"{listing.price or '—'} | {listing.city or '—'}")
                    time.sleep(args.pause)

            if collected >= args.limit:
                break

    print(f"\nСобрано {collected}, пропущено {skipped}, неудач {failed}")
    print(f"CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
