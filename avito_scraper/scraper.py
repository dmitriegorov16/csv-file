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

try:
    # Браузер нужен только веткам с Playwright. Разбор HTML и sitemap
    # работают без него, и fast_scrape импортирует отсюда общие вещи —
    # незачем требовать установленный браузер ради Listing и CSV_FIELDS.
    from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - зависит от окружения
    HAVE_PLAYWRIGHT = False
    Page = object

    class PWTimeoutError(Exception):
        pass

    def sync_playwright(*_args, **_kwargs):
        raise SystemExit("Нужен Playwright: pip install playwright")

try:
    import captcha_solver
except ImportError:  # pragma: no cover
    captcha_solver = None

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


def _proxy_config() -> Optional[dict]:
    """Собирает конфиг прокси для Playwright из переменных окружения.
    AVITO_PROXY_SERVER — например "http://host:3071" или "socks5://host:3072".
    AVITO_PROXY_USERNAME / AVITO_PROXY_PASSWORD — опционально, если прокси
    с авторизацией по логину/паролю (а не по IP)."""
    import os
    server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    if not server:
        return None
    config = {"server": server}
    username = os.environ.get("AVITO_PROXY_USERNAME", "").strip()
    password = os.environ.get("AVITO_PROXY_PASSWORD", "").strip()
    if username:
        config["username"] = username
    if password:
        config["password"] = password
    return config


def launch_browser(pw, headless: bool):
    # Позволяет указать путь к уже установленному Chromium через переменную
    # окружения (например, в песочницах с предустановленным браузером),
    # не трогая обычный playwright-managed браузер в Codespaces.
    import os
    executable_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    kwargs = {"headless": headless}
    if executable_path:
        kwargs["executable_path"] = executable_path
    proxy = _proxy_config()
    if proxy:
        kwargs["proxy"] = proxy
        print(f"[proxy] использую {proxy['server']}")
    return pw.chromium.launch(**kwargs)


BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")


def new_context(pw_browser, headless_ua: Optional[str] = None,
                block_resources: bool = True, mobile: bool = False):
    # При работе через мобильный прокси браузер тоже должен быть
    # телефоном: запрос с сотового адреса, представляющийся десктопным
    # Chrome под Windows, сам себе противоречит.
    if mobile:
        context = pw_browser.new_context(
            user_agent=IPHONE_UA,
            locale="ru-RU",
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
    else:
        context = pw_browser.new_context(
            user_agent=headless_ua or random.choice(USER_AGENTS),
            locale="ru-RU",
            viewport={"width": random.randint(1280, 1600),
                      "height": random.randint(800, 1000)},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
    # немного скрыть автоматизацию
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    if block_resources:
        # нам нужны только текст/цена/ссылки и URL картинки из meta-тега,
        # сами байты картинок/шрифтов/css не нужны — экономит основную
        # часть трафика (актуально при работе через платные прокси)
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES
            else route.continue_(),
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

def collect_links(start_url: str, pages: int, delay: tuple[float, float], headless: bool, block_resources: bool = True) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if URLS_FILE.exists():
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                seen.add(json.loads(line)["url"])

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless)
        context = new_context(browser, block_resources=block_resources)
        page = context.new_page()
        attach_diagnostics(page)

        added = 0
        with URLS_FILE.open("a", encoding="utf-8") as out:
            for page_num in range(1, pages + 1):
                url = paginate_url(start_url, page_num)
                print(f"[collect] страница {page_num}/{pages}: {url}")
                response = goto_with_backoff(page, url)
                if response is None:
                    print("  -> таймаут загрузки, пропускаю страницу")
                    continue

                if not try_unblock(page, delay):
                    print("  -> похоже на антибот/капчу, автоматически решить не удалось. "
                          "Останавливаюсь, попробуйте позже или запустите с --no-headless "
                          "и решите капчу вручную.")
                    break

                human_delay(*delay)
                wait_for_item_links(page)

                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    )
                except Exception:
                    # ещё одна навигация в процессе (SPA-редирект и т.п.) — даём
                    # странице осесть и пробуем один раз ещё
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
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


# --------------------------------------------------------------------------
# Шаг 1-альтернатива: сбор ссылок из sitemap
# --------------------------------------------------------------------------
#
# Avito публикует карты сайта для поисковиков, и они отдаются обычным HTTP
# без фаервола/капчи/прокси (проверено: robots.txt и sitemap отдаются даже
# с того IP, которому фронтенд показывает "Доступ ограничен"). В индексе
# есть карты вида item_<категория>_<id>_<N>.xml.gz — это ссылки на сами
# карточки объявлений, до ~50000 в одном файле.
#
# Это делает обход страниц поиска браузером ненужным: ссылки берём здесь,
# бесплатно и почти без трафика, а браузер тратим только на сами карточки.

SITEMAP_INDEX_URL = "https://www.avito.ru/sitemap/index.xml"
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")


def _http_get(url: str, timeout: int = 60) -> bytes:
    import urllib.request
    request = urllib.request.Request(url, headers={
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_locs(url: str) -> list[str]:
    """Скачивает sitemap (при необходимости распаковывает .gz) и возвращает
    все <loc> из него."""
    import gzip
    raw = _http_get(url)
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return LOC_RE.findall(raw.decode("utf-8", errors="replace"))


def collect_from_sitemap(category: Optional[str], max_urls: int) -> None:
    """Наполняет очередь ссылок (data/urls.jsonl) из карт сайта Avito.

    category — подстрока имени карты для фильтра (например "avtomobili"
    или "mebel"); если не задана, берутся все карты с карточками подряд.
    max_urls — сколько ссылок набрать за этот запуск."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    if URLS_FILE.exists():
        for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                seen.add(json.loads(line)["url"])
    print(f"[sitemap] в очереди уже есть ссылок: {len(seen)}")

    print(f"[sitemap] качаю индекс {SITEMAP_INDEX_URL}")
    all_maps = _fetch_locs(SITEMAP_INDEX_URL)

    # нас интересуют только карты с карточками объявлений (item_*),
    # canonical_serp_* и прочие — это категории и фильтры, не объявления
    item_maps = [m for m in all_maps if "/item_" in m]
    if category:
        item_maps = [m for m in item_maps if category in m]
    print(f"[sitemap] подходящих карт с объявлениями: {len(item_maps)}")
    if not item_maps:
        print("[sitemap] ничего не найдено — проверьте значение --category "
              "(это подстрока имени карты, например avtomobili, mebel, akvarium)")
        return

    random.shuffle(item_maps)

    added = 0
    with URLS_FILE.open("a", encoding="utf-8") as out:
        for sitemap_url in item_maps:
            if added >= max_urls:
                break
            print(f"[sitemap] {sitemap_url.rsplit('/', 1)[-1]}")
            try:
                locs = _fetch_locs(sitemap_url)
            except Exception as exc:
                print(f"  -> не смог скачать: {type(exc).__name__}: {exc}")
                continue

            found = 0
            for loc in locs:
                if added >= max_urls:
                    break
                loc = loc.split("?")[0]
                if ITEM_LINK_RE.match(loc) and loc not in seen:
                    seen.add(loc)
                    out.write(json.dumps({"url": loc}, ensure_ascii=False) + "\n")
                    added += 1
                    found += 1
            out.flush()
            print(f"  -> новых ссылок: {found} (всего добавлено: {added}/{max_urls})")

    print(f"[sitemap] готово. Добавлено новых ссылок: {added}. Всего в очереди: {len(seen)}")


def paginate_url(start_url: str, page_num: int) -> str:
    if page_num <= 1:
        return start_url
    sep = "&" if "?" in start_url else "?"
    return f"{start_url}{sep}p={page_num}"


def attach_diagnostics(page: Page) -> None:
    """Логирует фоновые запросы с ошибочным статусом (429/403/5xx) —
    именно такие XHR внутри SPA грузят реальные данные объявлений, и их
    429 не виден в статусе основного page.goto()."""
    def on_response(response):
        try:
            status = response.status
        except Exception:
            return
        if status == 429 or status >= 500 or status == 403:
            print(f"  [net] {status} {response.url[:100]}")

    page.on("response", on_response)


def goto_with_backoff(page: Page, url: str, max_attempts: int = 4, backoff_seconds: float = 25.0):
    """page.goto с отдельной обработкой 429 (Too Many Requests) — это не
    антибот-капча, а rate-limit конкретно на этом IP/прокси, капча его не
    снимает. При 429 ждём подольше и повторяем; возвращает Response
    последней попытки (может быть None, если это был таймаут)."""
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeoutError:
            return None
        except Exception as exc:
            # обрыв соединения и т.п. (особенно частое дело у нестабильных
            # бесплатных/публичных прокси) — не таймаут и не HTTP-ответ,
            # но тоже стоит попробовать ещё раз, а не падать всем скриптом
            print(f"  -> ошибка соединения (попытка {attempt}/{max_attempts}): "
                  f"{type(exc).__name__}: {str(exc)[:150]}")
            if attempt < max_attempts:
                time.sleep(min(backoff_seconds, 10))
                continue
            return None
        if response is not None and response.status == 429:
            print(f"  -> 429 Too Many Requests (попытка {attempt}/{max_attempts}), "
                  f"жду {backoff_seconds:.0f}с — это rate-limit по IP, не капча")
            if attempt < max_attempts:
                time.sleep(backoff_seconds)
            continue
        return response
    return response


# Заголовки, которые Avito ставит на своих страницах-заглушках фаервола:
# "Доступ ограничен: проблема с IP", "Доступ ограничен: проверка безопасности".
FIREWALL_TITLE_MARKERS = [
    "доступ ограничен",
    "проверка безопасности",
    "подтвердите, что вы не робот",
    "проверка браузера",
]


def is_blocked(page: Page) -> bool:
    """Заблокирована ли страница фаерволом Avito.

    ВАЖНО: нельзя искать слово "captcha" по всему HTML — на нормальной
    странице объявления оно встречается (например, в recaptcha-compat
    шиме), и тогда прекрасно загрузившаяся карточка ошибочно считается
    заблокированной. Поэтому проверяем только заголовок страницы и
    наличие самой формы фаервола."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if any(m in title for m in FIREWALL_TITLE_MARKERS):
        return True
    try:
        return page.query_selector(".js-firewall-form") is not None
    except Exception:
        return False


def wait_for_item_links(page: Page, timeout_ms: int = 15000) -> None:
    """Avito — SPA: сразу после domcontentloaded карточек объявлений на
    странице ещё нет, React дорисовывает их отдельным запросом. Ждём, пока
    появится хотя бы одна ссылка на объявление, прежде чем читать hrefs.
    Если за timeout_ms ничего не появилось — просто идём дальше с тем, что
    есть (страница могла быть пустой категорией и т.п.)."""
    try:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .some(a => /^https:\\/\\/www\\.avito\\.ru\\/.+_\\d+$/.test(a.href.split('?')[0]))""",
            timeout=timeout_ms,
        )
    except PWTimeoutError:
        pass


def try_unblock(page: Page, delay: tuple[float, float]) -> bool:
    """Если страница заблокирована и настроен RUCAPTCHA_API_KEY — пробует
    решить капчу. Возвращает True, если после попытки страница больше не
    выглядит заблокированной.

    solve_if_present() сам дожидается перезагрузки, которую Avito делает
    после успешной проверки (по cookie с очень коротким Max-Age) — поэтому
    здесь не делаем свой дополнительный page.reload(), это может опоздать
    и увидеть уже протухшую cookie."""
    if not is_blocked(page):
        return True
    # видно, что именно сработало — иначе непонятно, настоящий это фаервол
    # или ложное срабатывание детектора на нормальной странице
    try:
        print(f"  [blocked] title={page.title()!r} "
              f"firewall_form={page.query_selector('.js-firewall-form') is not None}")
    except Exception:
        pass
    if captcha_solver is not None and captcha_solver.solve_if_present(page):
        # после успешной проверки Avito может ещё редиректить/дохлопывать
        # SPA-навигацию — дадим странице осесть, иначе следующий же
        # eval_on_selector_all падает с "Execution context was destroyed"
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeoutError:
            pass
        human_delay(*delay)
        return True
    human_delay(*delay)
    return not is_blocked(page)


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


def parse_item(page: Page, url: str, item_id: int, delay: tuple[float, float]) -> Optional[Listing]:
    if goto_with_backoff(page, url) is None:
        print(f"  -> таймаут при загрузке {url}")
        return None

    if not try_unblock(page, delay):
        print(f"  -> заблокировано (антибот/капча), решить не удалось: {url}")
        return None

    # объявление могло быть снято с публикации
    if page.query_selector('[data-marker="item-view/closed-warning"]'):
        print(f"  -> объявление снято/недоступно: {url}")
        return None

    # карточка объявления тоже может дорисовываться JS уже после
    # domcontentloaded — ждём появления заголовка перед парсингом полей
    try:
        page.wait_for_selector(
            '[data-marker="item-view/title-info"], meta[property="og:title"]',
            timeout=10000,
        )
    except PWTimeoutError:
        pass

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


def scrape(limit: Optional[int], delay: tuple[float, float], headless: bool,
           block_resources: bool = True, mobile: bool = False) -> None:
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
        browser = launch_browser(pw, headless)
        context = new_context(browser, block_resources=block_resources, mobile=mobile)
        page = context.new_page()
        attach_diagnostics(page)

        with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()

            processed = 0
            for url in todo:
                print(f"[scrape] ({processed + 1}/{len(todo)}) {url}")
                listing = parse_item(page, url, state["next_id"], delay)
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
    p_collect.add_argument("--block-resources", action="store_true", default=True,
                            help="не грузить картинки/шрифты/css (экономит трафик, включено по умолчанию)")
    p_collect.add_argument("--no-block-resources", dest="block_resources", action="store_false")

    p_sitemap = sub.add_parser(
        "sitemap",
        help="набрать ссылки на объявления из карт сайта Avito (без браузера, прокси и капчи)")
    p_sitemap.add_argument("--category", default=None,
                           help="фильтр по имени карты, например avtomobili / mebel / akvarium "
                                "(по умолчанию — все категории подряд)")
    p_sitemap.add_argument("--max-urls", type=int, default=30000,
                           help="сколько ссылок набрать за этот запуск")

    p_scrape = sub.add_parser("scrape", help="обойти очередь ссылок и заполнить CSV")
    p_scrape.add_argument("--limit", type=int, default=None, help="сколько объявлений обработать за этот запуск")
    p_scrape.add_argument("--delay-min", type=float, default=2.0)
    p_scrape.add_argument("--delay-max", type=float, default=4.5)
    p_scrape.add_argument("--headless", action="store_true", default=True)
    p_scrape.add_argument("--no-headless", dest="headless", action="store_false")
    p_scrape.add_argument("--block-resources", action="store_true", default=True,
                            help="не грузить картинки/шрифты/css (экономит трафик, включено по умолчанию)")
    p_scrape.add_argument("--no-block-resources", dest="block_resources", action="store_false")
    p_scrape.add_argument("--mobile", action="store_true",
                          help="эмулировать iPhone: нужно при работе через мобильный прокси")

    args = parser.parse_args()

    if args.command == "collect":
        collect_links(args.start_url, args.pages, (args.delay_min, args.delay_max), args.headless, args.block_resources)
    elif args.command == "sitemap":
        collect_from_sitemap(args.category, args.max_urls)
    elif args.command == "scrape":
        scrape(args.limit, (args.delay_min, args.delay_max), args.headless,
               args.block_resources, args.mobile)


if __name__ == "__main__":
    main()
