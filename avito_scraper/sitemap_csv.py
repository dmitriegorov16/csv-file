#!/usr/bin/env python3
"""
CSV прямо из sitemap — без единого запроса к карточкам.

В картах Avito лежит не только адрес объявления, но и расширение
sitemap-image: фотография. Плюс дата изменения. А город и категорию мы
умеем доставать из самого адреса. Итого пять полей из десяти
заполняются бесплатно, мгновенно и без прокси, капчи и блокировок:

    id, url, image, city, category

Шестое, title, восстанавливается из slug обратной транслитерацией —
приблизительно: "prodam_akvarium_s_rybkami" -> "Продам аквариум с
рыбками". Для вещей выходит читаемо, для авто хуже ("2 0 at" вместо
"2.0 AT"), поэтому настоящий заголовок всё равно берётся со страницы,
когда до неё удаётся достучаться.

Оставшиеся четыре (price, content, description, address) на странице и
только на ней — sitemap их не содержит.

    python sitemap_csv.py --limit 30000
    python sitemap_csv.py --limit 1000 --out data/base.csv
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
import urllib.request

from scraper import CSV_FIELDS, DATA_DIR, OUTPUT_CSV, Listing
from url_meta import category_from_url, city_from_url, detransliterate

SITEMAP_INDEX = "https://www.avito.ru/sitemap/index.xml"
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")

# Разбираем по одному блоку <url>, а не всё сразу: если искать адрес и
# картинку одной регуляркой, необязательная группа с картинкой просто
# схлопывается в пустоту и фото теряется.
URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.DOTALL)
IMAGE_RE = re.compile(r"<image:loc>([^<]+)</image:loc>")


def parse_entries(body: str):
    """(адрес, картинка) для каждого объявления в карте."""
    for block in URL_BLOCK_RE.findall(body):
        location = LOC_RE.search(block)
        if not location:
            continue
        image = IMAGE_RE.search(block)
        yield location.group(1), (image.group(1) if image else "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sitemap reader)"}


def fetch(url: str, timeout: int = 90) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def title_from_slug(url: str) -> str:
    """Заголовок из последнего сегмента адреса.

    Настоящего заголовка в sitemap нет, но slug сделан из него же:
    убираем числовой id, переводим латиницу обратно в кириллицу и
    возвращаем в виде обычного предложения."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"_\d{6,}$", "", slug)
    if not slug:
        return ""
    words = []
    for word in slug.split("_"):
        if not word:
            continue
        # числа и латинские аббревиатуры транслитерировать не нужно
        words.append(word if re.fullmatch(r"[\d.,]+", word)
                     else detransliterate(word).lower())
    text = " ".join(w for w in words if w)
    return text[:1].upper() + text[1:]


def entries(limit: int, verbose: bool = True):
    """Пары (адрес, картинка) из карт объявлений, пока не наберётся limit."""
    index = fetch(SITEMAP_INDEX).decode("utf-8", "replace")
    maps = [m for m in LOC_RE.findall(index) if "/item_" in m]
    if verbose:
        print(f"карт объявлений в индексе: {len(maps)}")

    seen = set()
    produced = 0
    for number, map_url in enumerate(maps, 1):
        if produced >= limit:
            return
        try:
            body = fetch(map_url).decode("utf-8", "replace")
        except Exception as exc:
            if verbose:
                print(f"  [{number}] {map_url.rsplit('/', 1)[-1]}: "
                      f"не скачалась ({type(exc).__name__})")
            continue
        got = 0
        for url, image in parse_entries(body):
            if url in seen:
                continue
            seen.add(url)
            yield url, image
            got += 1
            produced += 1
            if produced >= limit:
                break
        if verbose:
            print(f"  [{number}] {map_url.rsplit('/', 1)[-1]}: "
                  f"+{got} (всего {produced})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=30000)
    parser.add_argument("--out", default=str(OUTPUT_CSV.with_name("base.csv")))
    parser.add_argument("--start-id", type=int, default=1)
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for offset, (url, image) in enumerate(entries(args.limit)):
            listing = Listing(
                id=args.start_id + offset,
                url=url,
                title=title_from_slug(url),
                content="",
                description="",
                image=image or "",
                price="",
                category=category_from_url(url),
                city=city_from_url(url),
                address="",
            )
            writer.writerow(listing.__dict__)
            written += 1

    print(f"\nГотово: {written} строк -> {args.out}")
    print("Заполнены id, url, title (приблизительно), image, category, city.")
    print("Пустые price, content, description, address берутся только со "
          "страницы объявления — их дозаполняет fast_scrape.py.")


if __name__ == "__main__":
    main()
