#!/usr/bin/env python3
"""
Быстрый сбор карточек прямыми HTTP-запросами — без браузера и капчи.

Логика всей связки:

    scraper.py sitemap  ->  ссылки на объявления (бесплатно, из карт сайта)
    proxy_pool.py       ->  прокси, с которых Avito отдаёт данные обычным
                            HTTP-запросом (проверка требует og:title в теле)
    fast_scrape.py      ->  качает карточки и пишет CSV

Почему это работает без капчи: страницы объявлений отрендерены на сервере
для поисковиков, поэтому все нужные поля лежат прямо в HTML (og-теги,
JSON-LD, разметка). Если IP не сожжён, страница приходит целиком обычным
запросом — а прокси с сожжёнными IP отсеиваются ещё на этапе проверки.

Прокси ротируются автоматически: адрес, который начал отдавать блокировку
или отвалился, откладывается, берётся следующий.

    python fast_scrape.py --limit 100
    python fast_scrape.py                 # вся очередь
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict

import proxy_pool
from scraper import (CSV_FIELDS, DATA_DIR, OUTPUT_CSV, URLS_FILE, Listing,
                     capitalize_city, load_state, save_state)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

FIREWALL_MARKERS = ("Доступ ограничен", "js-firewall-form", "проверка безопасности")

META_RE_CACHE: dict[str, re.Pattern] = {}
ITEM_ID_RE = re.compile(r"_(\d+)$")
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def meta_content(page_html: str, attr: str, value: str) -> str:
    """Достаёт content из <meta ...>, независимо от порядка атрибутов."""
    key = f"{attr}={value}"
    if key not in META_RE_CACHE:
        META_RE_CACHE[key] = re.compile(
            r'<meta[^>]+(?:%s=["\']%s["\'][^>]+content=["\'](.*?)["\']'
            r'|content=["\'](.*?)["\'][^>]+%s=["\']%s["\'])'
            % (attr, re.escape(value), attr, re.escape(value)),
            re.IGNORECASE | re.DOTALL)
    match = META_RE_CACHE[key].search(page_html)
    if not match:
        return ""
    return html_module.unescape(match.group(1) or match.group(2) or "").strip()


def strip_tags(fragment: str) -> str:
    return " ".join(html_module.unescape(TAG_RE.sub(" ", fragment)).split())


def block_text(page_html: str, marker: str) -> str:
    """Весь текст элемента с data-marker="...", включая вложенные теги.

    Наивный regex "до первого закрывающего тега" здесь не годится:
    описание объявления состоит из нескольких абзацев, и тогда терялось
    бы всё после первого </p>. Поэтому находим открывающий тег и идём по
    HTML, считая вложенность тегов того же имени."""
    match = re.search(r'<(\w+)[^>]*data-marker=["\']%s["\'][^>]*>' % re.escape(marker),
                      page_html)
    if not match:
        return ""
    tag = match.group(1)
    start = match.end()

    open_re = re.compile(r"<%s\b" % tag, re.IGNORECASE)
    close_re = re.compile(r"</%s\s*>" % tag, re.IGNORECASE)

    depth = 1
    position = start
    limit = min(len(page_html), start + 200000)
    while position < limit:
        next_open = open_re.search(page_html, position, limit)
        next_close = close_re.search(page_html, position, limit)
        if not next_close:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            position = next_open.end()
            continue
        depth -= 1
        if depth == 0:
            return strip_tags(page_html[start:next_close.start()])
        position = next_close.end()
    return strip_tags(page_html[start:limit])


def extract_json_ld(page_html: str) -> dict:
    for raw in JSON_LD_RE.findall(page_html):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") in ("Product", "Offer", "Vehicle", "Car"):
                return item
    return {}


def parse_html(page_html: str, url: str, item_id: int) -> Listing:
    """Чистая функция: HTML -> строка CSV. Тестируется без сети."""
    json_ld = extract_json_ld(page_html)
    offers = json_ld.get("offers") if isinstance(json_ld.get("offers"), dict) else {}

    title = (meta_content(page_html, "property", "og:title")
             or block_text(page_html, "item-view/title-info")
             or json_ld.get("name", ""))
    # Avito добавляет к og:title хвост вида " | Авито" — он не нужен
    title = re.sub(r"\s*\|\s*Авито\s*$", "", title).strip()

    content = (block_text(page_html, "item-view/item-description")
               or json_ld.get("description", ""))

    meta_description = meta_content(page_html, "name", "description")
    if meta_description:
        description = meta_description
    elif content:
        description = (content[:100] + "…") if len(content) > 100 else content
    else:
        description = ""

    image = (meta_content(page_html, "property", "og:image")
             or (json_ld.get("image") if isinstance(json_ld.get("image"), str) else ""))

    price = (str(offers.get("price", ""))
             or meta_content(page_html, "itemprop", "price")
             or block_text(page_html, "item-view/item-price"))
    price = re.sub(r"[^\d]", "", price)

    address = block_text(page_html, "item-view/item-address")

    city = ""
    address_data = offers.get("availableAtOrFrom") or json_ld.get("address")
    if isinstance(address_data, dict):
        locality = address_data.get("addressLocality")
        if isinstance(address_data.get("address"), dict):
            locality = locality or address_data["address"].get("addressLocality")
        city = locality or ""
    if not city and address:
        city = address.split(",")[0]
    city = capitalize_city(city)

    category = ""
    breadcrumbs = re.findall(r'itemprop=["\']name["\'][^>]*>([^<]{1,80})<', page_html)
    crumbs = [strip_tags(b) for b in breadcrumbs if strip_tags(b)]
    crumbs = [c for c in crumbs if c and c != title]
    if crumbs:
        category = crumbs[-1]
    if not category and isinstance(json_ld.get("category"), str):
        category = json_ld["category"]

    return Listing(
        id=item_id, url=url, title=title.strip(), content=content.strip(),
        description=description.strip(), image=image.strip(), price=price.strip(),
        category=category.strip(), city=city.strip(), address=address.strip(),
    )


class RotatingFetcher:
    """Качает страницы через список прокси, отбраковывая испортившиеся."""

    def __init__(self, proxies: list[str], timeout: int = 20):
        self.proxies = list(proxies)
        random.shuffle(self.proxies)
        self.timeout = timeout
        self.index = 0
        self.failures: dict[str, int] = {}

    @property
    def current(self) -> str:
        return self.proxies[self.index % len(self.proxies)]

    def rotate(self, reason: str) -> bool:
        bad = self.current
        self.failures[bad] = self.failures.get(bad, 0) + 1
        if self.failures[bad] >= 3:
            print(f"  [proxy] выбрасываю {bad} ({reason}, 3 неудачи подряд)")
            self.proxies.remove(bad)
            self.failures.pop(bad, None)
            if not self.proxies:
                return False
            self.index = self.index % len(self.proxies)
        else:
            self.index = (self.index + 1) % len(self.proxies)
        return True

    def get(self, url: str) -> tuple[str, str]:
        """Возвращает (html, ''), либо ('', причина ошибки)."""
        for _ in range(min(6, len(self.proxies) * 2)):
            server = self.current
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": server, "https": server}))
            try:
                with opener.open(urllib.request.Request(url, headers=HEADERS),
                                 timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if not self.rotate(f"HTTP {exc.code}"):
                    return "", "прокси кончились"
                continue
            except Exception as exc:
                if not self.rotate(type(exc).__name__):
                    return "", "прокси кончились"
                continue

            if any(marker in body for marker in FIREWALL_MARKERS):
                if not self.rotate("фаервол"):
                    return "", "прокси кончились"
                continue

            self.failures.pop(server, None)
            return body, ""
        return "", "не удалось получить страницу"


def ensure_proxies(needed: int) -> list[str]:
    proxies = proxy_pool.load_working()
    if proxies:
        print(f"[proxy] беру сохранённые: {len(proxies)}")
        return proxies
    print("[proxy] сохранённых нет — ищу рабочие")
    servers = proxy_pool.harvest()
    proxies = proxy_pool.find_working(servers, needed)
    if proxies:
        proxy_pool.save_working(proxies)
    return proxies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None,
                        help="сколько объявлений обработать за запуск")
    parser.add_argument("--proxies-needed", type=int, default=5,
                        help="сколько рабочих прокси искать, если сохранённых нет")
    parser.add_argument("--delay-min", type=float, default=0.5)
    parser.add_argument("--delay-max", type=float, default=1.5)
    args = parser.parse_args()

    if not URLS_FILE.exists():
        sys.exit("Нет очереди ссылок. Сначала: python scraper.py sitemap --category avtomobili")

    all_urls = [json.loads(line)["url"]
                for line in URLS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    state = load_state()
    done = set(state["done_urls"])
    todo = [u for u in all_urls if u not in done]
    if args.limit is not None:
        todo = todo[:args.limit]
    if not todo:
        print("Всё из очереди уже обработано.")
        return

    proxies = ensure_proxies(args.proxies_needed)
    if not proxies:
        sys.exit("Рабочих прокси не найдено. Запустите: python proxy_pool.py --limit 5000")

    fetcher = RotatingFetcher(proxies)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_CSV.exists()

    print(f"[scrape] к обработке: {len(todo)} (готово ранее: {len(done)})\n")
    ok_count = fail_count = 0

    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for number, url in enumerate(todo, 1):
            page_html, error = fetcher.get(url)
            if error:
                fail_count += 1
                print(f"  [{number}/{len(todo)}] ОШИБКА {error}: {url[:80]}")
                if error == "прокси кончились":
                    print("\nПрокси закончились. Запустите proxy_pool.py за новой порцией.")
                    break
                continue

            listing = parse_html(page_html, url, state["next_id"])
            if not listing.title:
                fail_count += 1
                print(f"  [{number}/{len(todo)}] пусто (нет заголовка): {url[:80]}")
            else:
                writer.writerow(asdict(listing))
                handle.flush()
                state["next_id"] += 1
                ok_count += 1
                print(f"  [{number}/{len(todo)}] {listing.title[:60]} | "
                      f"{listing.price or '—'} | {listing.city or '—'}")

            done.add(url)
            state["done_urls"] = sorted(done)
            save_state(state)
            time.sleep(random.uniform(args.delay_min, args.delay_max))

    print(f"\n[scrape] собрано: {ok_count}, неудач: {fail_count}. "
          f"Всего строк в {OUTPUT_CSV.name}: {state['next_id'] - 1}")


if __name__ == "__main__":
    main()
