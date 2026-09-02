#!/usr/bin/env python3
"""
Avito CSV scraper.

Собирает объявления с Avito через Playwright (Chromium) в два шага:

  1) collect  — обходит страницы поиска/каталога и складывает ссылки на
                объявления в файл очереди (data/urls.jsonl).
  2) scrape   — открывает каждую ссылку из очереди, вытаскивает поля и
                дописывает строки в итоговый CSV (data/avito.csv).

Оба шага возобновляемые: прогресс хранится в data/state.json, уже
обработанные ссылки не скачиваются повторно. Можно гонять маленькими
тестовыми партиями через --limit и постепенно докатывать до 30000.

Использование (см. README.md):

    python scraper.py collect --start-url "https://www.avito.ru/..." --pages 5
    python scraper.py scrape --limit 50 --headless
    python scraper.py scrape --limit 29950 --headless   # продолжит с того же места
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
URLS_FILE = DATA_DIR / "urls.jsonl"       # очередь ссылок на объявления
STATE_FILE = DATA_DIR / "state.json"      # прогресс (id-счётчик, обработанные url)
OUTPUT_CSV = DATA_DIR / "avito.csv"

CSV_FIELDS = [
    "id", "url", "title", "content", "description",
    "image", "price", "category", "city", "address",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

ITEM_LINK_RE = re.compile(r"^https://www\.avito\.ru/.+_\d+$")


# --------------------------------------------------------------------------
# Вспомогательные штуки
# --------------------------------------------------------------------------

def human_delay(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))


def new_context(pw_browser, headless_ua: Optional[str] = None):
    ua = headless_ua or random.choice(USER_AGENTS)
    context = pw_browser.new_context(
        user_agent=ua,
        locale="ru-RU",
        viewport={"width": random.randint(1280, 1600), "height": random.randint(800, 1000)},
        extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
    )
    # немного скрыть автоматизацию
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context


def capitalize_city(name: str) -> str:
    name = name.strip()
    if not name:
        return name
    # убираем частые префиксы вида "г. ", "г "
    name = re.sub(r"^(г\.|город)\s+", "", name, flags=re.IGNORECASE)
    return name[0].upper() + name[1:]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"next_id": 1, "done_urls": []}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Шаг 1: сбор ссылок на объявления со страниц поиска
# --------------------------------------------------------------------------

def collect_links(start_url: str, pages: int, delay: tuple[float, float], headless: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if URLS_FILE.exists():
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                seen.add(json.loads(line)["url"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = new_context(browser)
        page = context.new_page()

        added = 0
        with URLS_FILE.open("a", encoding="utf-8") as out:
            for page_num in range(1, pages + 1):
                url = paginate_url(start_url, page_num)
                print(f"[collect] страница {page_num}/{pages}: {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PWTimeoutError:
                    print("  -> таймаут загрузки, пропускаю страницу")
                    continue

                if is_blocked(page):
                    print("  -> похоже на антибот/капчу. Останавливаюсь, попробуйте позже "
                          "или запустите с --headless=false и решите капчу вручную.")
                    break

                human_delay(*delay)

                hrefs = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)"
                )
                found_on_page = 0
                for href in hrefs:
                    href = href.split("?")[0]
                    if ITEM_LINK_RE.match(href) and href not in seen:
                        seen.add(href)
                        out.write(json.dumps({"url": href}, ensure_ascii=False) + "\n")
                        added += 1
                        found_on_page += 1
                out.flush()
                print(f"  -> новых ссылок: {found_on_page} (всего в очереди: {len(seen)})")

                human_delay(*delay)

        context.close()
        browser.close()
    print(f"[collect] готово. Добавлено новых ссылок: {added}. Всего в очереди: {len(seen)}")


def paginate_url(start_url: str, page_num: int) -> str:
    if page_num <= 1:
        return start_url
    sep = "&" if "?" in start_url else "?"
    return f"{start_url}{sep}p={page_num}"


def is_blocked(page: Page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    content = ""
    try:
        content = page.content().lower()
    except Exception:
        pass
    markers = ["captcha", "доступ ограничен", "подтвердите, что вы не робот", "проверка браузера"]
    return any(m in title or m in content for m in markers)


# --------------------------------------------------------------------------
# Шаг 2: разбор карточки объявления
# --------------------------------------------------------------------------

@dataclass
class Listing:
    id: int
    url: str
    title: str
    content: str
    description: str
    image: str
    price: str
    category: str
    city: str
    address: str


def text_or_empty(page: Page, selector: str) -> str:
    try:
        el = page.query_selector(selector)
        if el:
            return el.inner_text().strip()
    except Exception:
        pass
    return ""


def attr_or_empty(page: Page, selector: str, attr: str) -> str:
    try:
        el = page.query_selector(selector)
        if el:
            val = el.get_attribute(attr)
            return (val or "").strip()
    except Exception:
        pass
    return ""


def extract_json_ld(page: Page) -> dict:
    """Достаём первый JSON-LD блок типа Product/Offer, если он есть на странице."""
    try:
        scripts = page.query_selector_all('script[type="application/ld+json"]')
    except Exception:
        return {}
    for s in scripts:
        try:
            data = json.loads(s.inner_text())
        except Exception:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") in ("Product", "Offer"):
                    return item
        elif isinstance(data, dict) and data.get("@type") in ("Product", "Offer"):
            return data
    return {}


def extract_breadcrumbs(page: Page) -> list[str]:
    # приоритет — микроразметка schema.org, она стабильнее вёрстки/классов
    items = page.query_selector_all(
        '[itemtype="http://schema.org/ListItem"] [itemprop="name"], '
        '[itemtype="https://schema.org/ListItem"] [itemprop="name"]'
    )
    names = [i.inner_text().strip() for i in items if i.inner_text().strip()]
    if names:
        return names
    # запасной вариант — data-marker брейдкрамбов Авито
    items = page.query_selector_all('[data-marker^="breadcrumbs"] a, [data-marker^="breadcrumbs"] span')
    return [i.inner_text().strip() for i in items if i.inner_text().strip()]


def parse_item(page: Page, url: str, item_id: int) -> Optional[Listing]:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeoutError:
        print(f"  -> таймаут при загрузке {url}")
        return None

    if is_blocked(page):
        print(f"  -> заблокировано (антибот/капча) на {url}")
        return None

    # объявление могло быть снято с публикации
    if page.query_selector('[data-marker="item-view/closed-warning"]'):
        print(f"  -> объявление снято/недоступно: {url}")
        return None

    json_ld = extract_json_ld(page)

    title = (
        text_or_empty(page, '[data-marker="item-view/title-info"]')
        or attr_or_empty(page, 'meta[property="og:title"]', "content")
        or json_ld.get("name", "")
    )

    content = (
        text_or_empty(page, '[data-marker="item-view/item-description"]')
        or json_ld.get("description", "")
    )

    meta_description = attr_or_empty(page, 'meta[name="description"]', "content")
    if meta_description:
        description = meta_description
    elif content:
        description = (content[:100] + "…") if len(content) > 100 else content
    else:
        description = ""

    image = (
        attr_or_empty(page, 'meta[property="og:image"]', "content")
        or attr_or_empty(page, '[data-marker="item-view/gallery"] img', "src")
    )

    price = (
        text_or_empty(page, '[data-marker="item-view/item-price"]')
        or attr_or_empty(page, 'meta[itemprop="price"]', "content")
    )
    price = re.sub(r"[^\d]", "", price)

    breadcrumbs = extract_breadcrumbs(page)
    # первая крошка обычно "Все объявления", последняя — сама карточка/заголовок;
    # категория — то, что осталось между ними
    category_crumbs = [b for b in breadcrumbs if b and b != title]
    category = category_crumbs[-1] if category_crumbs else ""

    address_text = (
        text_or_empty(page, '[data-marker="item-view/item-address"]')
        or text_or_empty(page, '[data-marker="item-view/address"]')
    )

    address = address_text
    city = ""
    postal = json_ld.get("address") if isinstance(json_ld.get("address"), dict) else None
    if postal and postal.get("addressLocality"):
        city = postal["addressLocality"]
    elif address_text:
        city = address_text.split(",")[0]
    city = capitalize_city(city)

    return Listing(
        id=item_id,
        url=url,
        title=title.strip(),
        content=content.strip(),
        description=description.strip(),
        image=image.strip(),
        price=price.strip(),
        category=category.strip(),
        city=city.strip(),
        address=address.strip(),
    )


def scrape(limit: Optional[int], delay: tuple[float, float], headless: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not URLS_FILE.exists():
        print("Нет очереди ссылок. Сначала запустите: python scraper.py collect ...")
        sys.exit(1)

    all_urls = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            all_urls.append(json.loads(line)["url"])

    state = load_state()
    done = set(state["done_urls"])
    todo = [u for u in all_urls if u not in done]
    if limit is not None:
        todo = todo[:limit]

    if not todo:
        print("Все ссылки из очереди уже обработаны. Запустите collect за новой порцией.")
        return

    print(f"[scrape] к обработке в этом запуске: {len(todo)} (уже готово ранее: {len(done)})")

    write_header = not OUTPUT_CSV.exists()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = new_context(browser)
        page = context.new_page()

        with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()

            processed = 0
            for url in todo:
                print(f"[scrape] ({processed + 1}/{len(todo)}) {url}")
                listing = parse_item(page, url, state["next_id"])
                human_delay(*delay)

                if listing is None:
                    # всё равно помечаем как обработанное, чтобы не биться в него снова
                    done.add(url)
                    state["done_urls"] = sorted(done)
                    save_state(state)
                    processed += 1
                    continue

                writer.writerow(asdict(listing))
                f.flush()

                state["next_id"] += 1
                done.add(url)
                state["done_urls"] = sorted(done)
                save_state(state)

                processed += 1

        context.close()
        browser.close()

    print(f"[scrape] готово. Обработано в этом запуске: {processed}. "
          f"Всего строк в {OUTPUT_CSV.name}: {state['next_id'] - 1}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="собрать ссылки на объявления со страниц поиска")
    p_collect.add_argument("--start-url", required=True, help="URL поиска/категории Avito")
    p_collect.add_argument("--pages", type=int, default=5, help="сколько страниц листинга обойти")
    p_collect.add_argument("--delay-min", type=float, default=1.5)
    p_collect.add_argument("--delay-max", type=float, default=3.5)
    p_collect.add_argument("--headless", action="store_true", default=True)
    p_collect.add_argument("--no-headless", dest="headless", action="store_false")

    p_scrape = sub.add_parser("scrape", help="обойти очередь ссылок и заполнить CSV")
    p_scrape.add_argument("--limit", type=int, default=None, help="сколько объявлений обработать за этот запуск")
    p_scrape.add_argument("--delay-min", type=float, default=2.0)
    p_scrape.add_argument("--delay-max", type=float, default=4.5)
    p_scrape.add_argument("--headless", action="store_true", default=True)
    p_scrape.add_argument("--no-headless", dest="headless", action="store_false")

    args = parser.parse_args()

    if args.command == "collect":
        collect_links(args.start_url, args.pages, (args.delay_min, args.delay_max), args.headless)
    elif args.command == "scrape":
        scrape(args.limit, (args.delay_min, args.delay_max), args.headless)


if __name__ == "__main__":
    main()
