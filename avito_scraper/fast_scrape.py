#!/usr/bin/env python3
"""
Сбор карточек Avito прямыми HTTP-запросами через пул прокси.

Главный принцип: проверка прокси и полезная работа — одно и то же
действие. Отдельная фаза "проверим 4000 адресов, потом начнём собирать"
бессмысленна: она идёт минуты, а бесплатный прокси за это время успевает
умереть, и к началу сбора список уже протух. Поэтому здесь просто берётся
очередной адрес и через него качается настоящая карточка:

    получилось  -> готовая строка CSV
    не вышло    -> адрес штрафуется, берём следующий

Ни один запрос не тратится впустую, окна протухания не существует.

Потоки работают параллельно, каждый со своим прокси, поэтому нестабильность
отдельных адресов перестаёт быть проблемой: пул большой, живые находятся
сами собой. Когда живых остаётся мало, список докачивается на ходу.

    python fast_scrape.py --limit 100          # первые 100 из очереди
    python fast_scrape.py                      # вся очередь
    python fast_scrape.py --workers 40         # больше параллелизма
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Optional

import proxy_pool
from scraper import (CSV_FIELDS, DATA_DIR, OUTPUT_CSV, URLS_FILE, Listing,
                     capitalize_city)
from url_meta import category_from_url, city_from_url

try:
    # brotli: без него нельзя обещать серверу Accept-Encoding: br —
    # он ответит сжатием, которое мы не распакуем, и полноценная
    # страница превратится в мусор, который код примет за пустую.
    import brotli  # noqa: F401
    HAVE_BROTLI = True
except ImportError:
    try:
        import brotlicffi  # noqa: F401
        HAVE_BROTLI = True
    except ImportError:
        HAVE_BROTLI = False

DONE_FILE = DATA_DIR / "done.txt"          # append-only: по строке на ссылку
COUNTER_FILE = DATA_DIR / "counter.json"   # только следующий id

# Полный набор заголовков, как у настоящего браузера. Трёх штук
# (User-Agent/Accept/Accept-Language) мало: живой Chrome шлёт ещё
# Sec-Fetch-*, Upgrade-Insecure-Requests, Accept-Encoding и client hints,
# и их отсутствие само по себе выдаёт автоматику.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",   # br добавляется ниже, если есть чем распаковать
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Safari на iOS: у него свой набор — client hints (sec-ch-ua) он не шлёт
# вовсе, и прислать их с айфонного User-Agent значит выдать подделку.
MOBILE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

if HAVE_BROTLI:
    HEADERS["Accept-Encoding"] = "gzip, deflate, br"
    MOBILE_HEADERS["Accept-Encoding"] = "gzip, deflate, br"

FIREWALL_MARKERS = ("Доступ ограничен", "js-firewall-form", "проверка безопасности")

META_RE_CACHE: dict = {}
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")

# Картинки объявления лежат на своей CDN. Отдельным шаблоном они
# отличаются от иконок сайта: og:image у мобильной версии — это
# touch-icon, а не фотография товара.
# Одного домена avito.st мало: там же лежат шрифты и статика, и первым
# совпадением оказывался .woff2. Нужна именно фотография: путь /image/
# или расширение картинки, и точно не из /assets/.
PHOTO_RE = re.compile(
    r'https://[\w.-]*avito\.st/[^"\'\s\\<>]*?'
    r'(?:/image/[^"\'\s\\<>]+|[^"\'\s\\<>]+\.(?:jpe?g|png|webp))',
    re.IGNORECASE)
JUNK_IMAGE_RE = re.compile(
    r"(icon|logo|placeholder|sprite|favicon|/assets/|/fonts/|\.svg|\.woff)",
    re.IGNORECASE)

# Общесайтовое описание, которое Avito ставит, когда своего у страницы нет.
# Записать его в поле description значило бы получить 30000 одинаковых строк.
BOILERPLATE_DESCRIPTIONS = (
    "объявления на сайте авито",
    "объявления о продаже",
    "avito.ru",
)


# --------------------------------------------------------------------------
# Разбор HTML: чистые функции, тестируются без сети (см. test_parse.py)
# --------------------------------------------------------------------------

def meta_content(page_html: str, attr: str, value: str) -> str:
    key = f"{attr}={value}"
    if key not in META_RE_CACHE:
        # Без DOTALL и без ".*?": на странице в 700 КБ одной строкой такая
        # регулярка при отсутствии совпадения уходит в катастрофический
        # перебор и считает минутами. Содержимое атрибута не может
        # содержать кавычку, которой закрыто, поэтому [^"\']* и точнее,
        # и линейно по времени.
        META_RE_CACHE[key] = re.compile(
            r'<meta[^>]{0,400}?(?:%s=["\']%s["\'][^>]{0,400}?content=["\']([^"\']*)["\']'
            r'|content=["\']([^"\']*)["\'][^>]{0,400}?%s=["\']%s["\'])'
            % (attr, re.escape(value), attr, re.escape(value)),
            re.IGNORECASE)
    match = META_RE_CACHE[key].search(page_html)
    if not match:
        return ""
    return html_module.unescape(match.group(1) or match.group(2) or "").strip()


def strip_tags(fragment: str) -> str:
    return " ".join(html_module.unescape(TAG_RE.sub(" ", fragment)).split())


def block_text(page_html: str, marker: str) -> str:
    """Текст элемента с data-marker, включая вложенные теги."""
    return strip_tags(block_html(page_html, marker))


def block_html(page_html: str, marker: str) -> str:
    """Внутренний HTML элемента с data-marker.

    Наивное "до первого закрывающего тега" тут не годится: описание
    состоит из нескольких абзацев, и всё после первого </p> терялось бы.
    Разметку возвращаем как есть — хлебные крошки надо разбирать по
    отдельным ссылкам, а из плоского текста они уже не разделяются."""
    match = re.search(r'<(\w+)[^>]*data-marker=["\']%s["\'][^>]*>' % re.escape(marker),
                      page_html)
    if not match:
        return ""
    tag, start = match.group(1), match.end()
    open_re = re.compile(r"<%s\b" % tag, re.IGNORECASE)
    close_re = re.compile(r"</%s\s*>" % tag, re.IGNORECASE)
    depth, position = 1, start
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
            return page_html[start:next_close.start()]
        position = next_close.end()
    return page_html[start:limit]


LINK_TEXT_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)

# В крошках первым идёт сам сайт и регион — категорией они не являются.
NOT_A_CATEGORY = {"авито", "главная", "все объявления", "объявления"}


def breadcrumbs(page_html: str) -> list:
    """Хлебные крошки: ['Авито', 'Волгоград', 'Транспорт', 'Автомобили'].

    Единственное место, где на мобильной странице есть категория:
    itemprop-разметки там нет вовсе, JSON-LD тоже."""
    inner = block_html(page_html, "breadcrumbs")
    if not inner:
        return []
    items = [strip_tags(chunk) for chunk in LINK_TEXT_RE.findall(inner)]
    return [item for item in items if item]


def extract_json_ld(page_html: str) -> dict:
    for raw in JSON_LD_RE.findall(page_html):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") in ("Product", "Offer", "Vehicle", "Car"):
                return item
    return {}


def pick_image(page_html: str, json_ld: dict) -> str:
    """Фотография объявления, а не иконка сайта."""
    candidates = [meta_content(page_html, "property", "og:image")]
    ld_image = json_ld.get("image")
    if isinstance(ld_image, str):
        candidates.append(ld_image)
    elif isinstance(ld_image, list):
        candidates.extend(i for i in ld_image if isinstance(i, str))
    for candidate in candidates:
        if candidate and not JUNK_IMAGE_RE.search(candidate):
            return candidate
    # og: нет или там touch-icon — ищем ссылку на CDN фотографий прямо
    # в разметке: и в <img src>, и во встроенном JSON состояния.
    for match in PHOTO_RE.finditer(page_html):
        candidate = match.group(0).replace("\\/", "/")
        if not JUNK_IMAGE_RE.search(candidate):
            return candidate
    return ""


def pick_description(page_html: str, content: str) -> str:
    """meta description, если она про это объявление, иначе обрезка текста."""
    meta_description = meta_content(page_html, "name", "description")
    lowered = meta_description.lower()
    generic = any(marker in lowered for marker in BOILERPLATE_DESCRIPTIONS)
    if meta_description and not generic and len(meta_description) > 30:
        return meta_description
    if content:
        return (content[:100] + "…") if len(content) > 100 else content
    return meta_description


def parse_html(page_html: str, url: str, item_id: int) -> Listing:
    json_ld = extract_json_ld(page_html)
    offers = json_ld.get("offers") if isinstance(json_ld.get("offers"), dict) else {}

    title = (meta_content(page_html, "property", "og:title")
             or block_text(page_html, "item-view/title-info")
             or json_ld.get("name", ""))
    title = re.sub(r"\s*\|\s*Авито\s*$", "", title).strip()

    content = (block_text(page_html, "item-view/item-description")
               or json_ld.get("description", ""))

    description = pick_description(page_html, content)
    image = pick_image(page_html, json_ld)

    price = (str(offers.get("price", ""))
             or meta_content(page_html, "itemprop", "price")
             or block_text(page_html, "item-view/item-price"))
    price = re.sub(r"[^\d]", "", price)

    address = (block_text(page_html, "item-view/item-address")
               or block_text(page_html, "delivery/location")
               or block_text(page_html, "item-address"))
    if not address:
        address_ld = json_ld.get("address")
        if isinstance(address_ld, str):
            address = address_ld
        elif isinstance(address_ld, dict):
            address = address_ld.get("streetAddress", "") or ""

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

    crumbs = breadcrumbs(page_html) or [
        strip_tags(b) for b in
        re.findall(r'itemprop=["\']name["\'][^>]*>([^<]{1,80})<', page_html)]
    crumbs = [c for c in crumbs
              if c and c != title and c.lower() not in NOT_A_CATEGORY]
    # последняя крошка — это часто сама модель ("Kia"), а не раздел;
    # берём последнюю, которая совпала с известным разделом, иначе просто
    # последнюю: раздел всегда стоит правее региона
    if not city and len(crumbs) > 1:
        city = crumbs[0]
        crumbs = crumbs[1:]
    category = crumbs[-1] if crumbs else ""
    if not category and isinstance(json_ld.get("category"), str):
        category = json_ld["category"]

    # Разметка у мобильной версии беднее, и город с категорией в ней часто
    # отсутствуют. В ссылке они есть всегда — берём оттуда, но только как
    # запасной источник: то, что написано на странице, точнее.
    city = city or city_from_url(url)
    category = category or category_from_url(url)

    return Listing(
        id=item_id, url=url, title=title.strip(), content=content.strip(),
        description=description.strip(), image=image.strip(), price=price.strip(),
        category=category.strip(), city=city.strip(), address=address.strip(),
    )


# --------------------------------------------------------------------------
# Прогресс: append-only, чтобы не переписывать список на 30000 строк
# --------------------------------------------------------------------------

def load_done() -> set:
    done = set()
    if DONE_FILE.exists():
        done.update(line.strip() for line in
                    DONE_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
    # перенос прогресса со старого формата (весь список внутри state.json)
    old_state = DATA_DIR / "state.json"
    if old_state.exists():
        try:
            data = json.loads(old_state.read_text(encoding="utf-8"))
            done.update(data.get("done_urls", []))
        except Exception:
            pass
    return done


def load_counter(fallback: int = 1) -> int:
    if COUNTER_FILE.exists():
        try:
            return int(json.loads(COUNTER_FILE.read_text(encoding="utf-8"))["next_id"])
        except Exception:
            pass
    old_state = DATA_DIR / "state.json"
    if old_state.exists():
        try:
            return int(json.loads(old_state.read_text(encoding="utf-8"))["next_id"])
        except Exception:
            pass
    return fallback


def save_counter(next_id: int) -> None:
    COUNTER_FILE.write_text(json.dumps({"next_id": next_id}), encoding="utf-8")


# --------------------------------------------------------------------------
# Пул прокси: без предварительной проверки, отбраковка по ходу дела
# --------------------------------------------------------------------------

class ProxyRing:
    """Кольцо прокси с приоритетом.

    priority — свои, приватные адреса. Их мало, но они на порядок ценнее
    публичных, поэтому пока жив хоть один, работаем только ими: если
    просто перемешать их с тремя тысячами публичных, на них придётся
    десятая доля процента запросов и мы так и не узнаем, годятся ли они."""

    def __init__(self, servers: list, strikes: int = 2, priority: Optional[list] = None,
                 cooldown: float = 0.0):
        self.priority = list(priority or [])
        rest = [s for s in servers if s not in set(self.priority)]
        random.shuffle(rest)
        self.servers = rest
        self.strikes = strikes
        self.cooldown = cooldown
        self.failures: dict = {}
        self.last_used: dict = {}
        self.lock = threading.Lock()
        self.position = 0
        self.priority_position = 0

    def _pick(self) -> Optional[str]:
        pool = self.priority if self.priority else self.servers
        if not pool:
            return None
        # ищем адрес, который достаточно "остыл": частые запросы с одного
        # IP — это ровно то, за что Avito банит, а приватных адресов мало
        # и терять их нельзя
        now = time.monotonic()
        for _ in range(len(pool)):
            if self.priority:
                self.priority_position = (self.priority_position + 1) % len(pool)
                server = pool[self.priority_position]
            else:
                self.position = (self.position + 1) % len(pool)
                server = pool[self.position]
            if now - self.last_used.get(server, 0.0) >= self.cooldown:
                self.last_used[server] = now
                return server
        return None

    def take(self, wait: float = 30.0) -> Optional[str]:
        """Отдаёт остывший прокси, при необходимости подождав."""
        deadline = time.monotonic() + wait
        while True:
            with self.lock:
                if not self.priority and not self.servers:
                    return None
                server = self._pick()
                if server is not None:
                    return server
            if time.monotonic() > deadline:
                return None
            time.sleep(0.25)

    def punish(self, server: str) -> None:
        with self.lock:
            self.failures[server] = self.failures.get(server, 0) + 1
            if self.failures[server] < self.strikes:
                return
            for pool in (self.priority, self.servers):
                if server in pool:
                    pool.remove(server)
                    self.failures.pop(server, None)
                    break

    def reward(self, server: str) -> None:
        with self.lock:
            self.failures.pop(server, None)

    def refill(self, servers: list) -> int:
        with self.lock:
            known = set(self.servers)
            fresh = [s for s in servers if s not in known]
            random.shuffle(fresh)
            self.servers.extend(fresh)
            return len(fresh)

    @property
    def alive(self) -> int:
        with self.lock:
            return len(self.priority) + len(self.servers)

    @property
    def using_private(self) -> bool:
        with self.lock:
            return bool(self.priority)


try:
    import requests
    HAVE_REQUESTS = True
except ImportError:            # без requests остаются только HTTP-прокси
    HAVE_REQUESTS = False


try:
    import socks  # noqa: F401  (PySocks — нужен requests для socks5://)
    HAVE_SOCKS = True
except ImportError:
    HAVE_SOCKS = False

try:
    # curl_cffi шлёт запросы с TLS-отпечатком настоящего Chrome, оставаясь
    # обычным HTTP-клиентом. Голый requests палится на первом же
    # рукопожатии: набор шифров и расширений у него не браузерный, и
    # антибот это видит ещё до того, как посмотрит на заголовки.
    from curl_cffi import requests as cffi_requests
    HAVE_CFFI = True
except ImportError:
    HAVE_CFFI = False


def cffi_profiles() -> list:
    """Имена профилей, которые понимает установленная версия curl_cffi.

    В разных версиях они пишутся по-разному (safari18_4_ios против
    safari184_ios), и неверное имя даёт ImpersonateError ещё до того, как
    запрос уйдёт в сеть — поэтому проверяем заранее."""
    if not HAVE_CFFI:
        return []
    try:
        import typing
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        return list(typing.get_args(BrowserTypeLiteral))
    except Exception:
        pass
    try:
        from curl_cffi.requests import BrowserType
        return [b.value for b in BrowserType]
    except Exception:
        return []


def _proxy_dict(server: str) -> dict:
    # socks5h означает "резолвить DNS на стороне прокси" — так надёжнее
    # и не светит наружу, какие домены мы запрашиваем
    if server.startswith("socks5://"):
        server = "socks5h://" + server[len("socks5://"):]
    return {"http": server, "https": server}


# Только последнее вхождение и только в конце логина. У провайдеров
# встречается "...-hold-session-session-XXXX", и подмена по первому
# совпадению ломала строку, оставляя старую сессию нетронутой.
SESSION_RE = re.compile(r"(session-)([A-Za-z0-9]+)$")


class RotatingSession:
    """Мобильный прокси с ротацией IP через смену сессии.

    Измерено на живом трафике: с мобильного IP Avito отдаёт страницу, но
    буквально следующий запрос с того же адреса ловит 429, а дальше 439.
    То есть один IP — это одна-две карточки, и весь сбор держится на
    быстрой смене адреса.

    У таких прокси IP привязан к строке сессии в логине: другая строка —
    другой адрес выхода. Поэтому ротация здесь бесплатная и мгновенная,
    достаточно подставить новое случайное значение.

    Логин берётся как шаблон: либо с явным {session}, либо (как в выдаче
    провайдеров) с готовым куском session-XXXX, который и подменяется.
    """

    def __init__(self, server: str, username: str, password: str):
        self.server = server.split("://")[-1]
        self.scheme = server.split("://")[0] if "://" in server else "http"
        self.password = password
        if "{session}" in username:
            self.template = username
        elif SESSION_RE.search(username):
            self.template = SESSION_RE.sub(r"\1{session}", username, count=1)
        else:
            # ротировать нечего — работаем одним адресом
            self.template = username
        self.rotatable = "{session}" in self.template

    def make(self) -> str:
        session = f"{random.randrange(16 ** 12):012x}"
        username = self.template.format(session=session) if self.rotatable else self.template
        return (f"{self.scheme}://{urllib.parse.quote(username, safe='')}:"
                f"{urllib.parse.quote(self.password, safe='')}@{self.server}")


bytes_downloaded = 0
bytes_lock = threading.Lock()
saved_empty = False

# статистика по адресам выхода: сколько их вообще и какие проходят
ip_stats: dict = {}
ip_stats_lock = threading.Lock()


def exit_ip(server: str, timeout: int = 15) -> str:
    """Реальный IP, с которого уходит запрос через этот прокси.

    Нужен, чтобы видеть, работает ли ротация и насколько велик пул: если
    на десять разных сессий приходится три адреса, дело не в настройках,
    а в том, что у провайдера столько и есть."""
    try:
        if HAVE_REQUESTS:
            response = requests.get("https://ipinfo.io/json", proxies=_proxy_dict(server),
                                    timeout=timeout)
            data = response.json()
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(_proxy_dict(server)))
            with opener.open("https://ipinfo.io/json", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        org = data.get("org", "?")
        return f"{data.get('ip', '?')} ({org})"
    except Exception as exc:
        return f"? ({type(exc).__name__})"


def note_ip(ip: str, ok: bool) -> None:
    with ip_stats_lock:
        stat = ip_stats.setdefault(ip, {"ok": 0, "fail": 0})
        stat["ok" if ok else "fail"] += 1


def _count_bytes(size: int) -> None:
    global bytes_downloaded
    with bytes_lock:
        bytes_downloaded += size


def fetch(server: Optional[str], url: str, timeout: int, head_bytes: int = 0,
          impersonate: str = "", mobile: bool = False):
    """Возвращает (html, '') либо ('', причина).

    SOCKS-прокси качаются через requests+PySocks: urllib их не умеет
    вообще, а именно они для нас важнее всего — обычные HTTP-прокси часто
    не поддерживают CONNECT, без которого HTTPS (а Avito только по HTTPS)
    невозможен в принципе.

    head_bytes > 0 — качать только начало страницы Range-запросом. Все
    нужные поля (og-теги, JSON-LD) лежат в <head>, то есть в первых
    десятках килобайт, а весь документ весит в разы больше. На платном
    резидентском трафике это прямая экономия: разница между "хватит
    гигабайта" и "не хватит".
    """
    if server and server.startswith("socks") and not HAVE_REQUESTS:
        return "", "нет requests для socks"

    if impersonate and HAVE_CFFI:
        try:
            response = cffi_requests.get(
                url, headers={"Range": f"bytes=0-{head_bytes - 1}"} if head_bytes else None,
                proxies=_proxy_dict(server) if server else None,
                impersonate=impersonate, timeout=timeout)
            if response.status_code not in (200, 206):
                return "", f"HTTP {response.status_code}"
            _count_bytes(len(response.content))
            body = response.text
        except Exception as exc:
            return "", type(exc).__name__
        if any(marker in body for marker in FIREWALL_MARKERS):
            return "", "фаервол"
        if "og:title" not in body:
            return "", "без данных"
        return body, ""

    headers = dict(MOBILE_HEADERS if mobile else HEADERS)
    if head_bytes:
        headers["Range"] = f"bytes=0-{head_bytes - 1}"
    proxies = _proxy_dict(server) if server else None

    try:
        if HAVE_REQUESTS:
            response = requests.get(url, headers=headers, proxies=proxies,
                                    timeout=timeout, allow_redirects=True)
            # 206 — сервер отдал запрошенный кусок, 200 — прислал всё целиком
            if response.status_code not in (200, 206):
                return "", f"HTTP {response.status_code}"
            _count_bytes(len(response.content))
            body = response.text
        else:
            handlers = [urllib.request.ProxyHandler(proxies)] if proxies else []
            opener = urllib.request.build_opener(*handlers)
            with opener.open(urllib.request.Request(url, headers=headers),
                             timeout=timeout) as response:
                raw = response.read()
            _count_bytes(len(raw))
            body = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return "", f"HTTP {exc.code}"
    except Exception as exc:
        return "", type(exc).__name__
    if any(marker in body for marker in FIREWALL_MARKERS):
        return "", "фаервол"
    # Раньше здесь стояло "нет og:title — значит не данные", и это выбрасывало
    # настоящие страницы: часть карточек Avito отдаётся без og-тегов, хотя
    # разметка объявления в них есть. Признаком успеха считаем отсутствие
    # блокировки и осмысленный объём, а разбираться с полями — дело парсера.
    if len(body) < 20000 and "item-view" not in body:
        # 200, но объявления в ответе нет. Надо понять, что это:
        # снятое объявление, редирект или что-то ещё — сохраняем первую
        # такую страницу, дальше разбираем глазами
        global saved_empty
        if not saved_empty:
            saved_empty = True
            try:
                (DATA_DIR / "empty_200.html").write_text(body, encoding="utf-8")
                print(f"  [!] 200 без данных, страница сохранена в "
                      f"{DATA_DIR / 'empty_200.html'}")
            except Exception:
                pass
        return "", "без данных"
    return body, ""


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=25)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--attempts", type=int, default=6,
                        help="сколько прокси перепробовать на одну карточку")
    parser.add_argument("--only-mine", action="store_true",
                        help="только свои прокси из data/my_proxies.txt, без публичных")
    parser.add_argument("--head-bytes", type=int, default=0,
                        help="качать только первые N байт страницы (Range-запрос); "
                             "нужные поля лежат в <head>, это экономит платный трафик. Разумно 60000")
    parser.add_argument("--proxy-list-url", default="",
                        help="ссылка провайдера, отдающая список прокси текстом; "
                             "можно задать и через AVITO_PROXY_LIST_URL")
    parser.add_argument("--strikes", type=int, default=2,
                        help="сколько неудач подряд до выбрасывания адреса из пула")
    parser.add_argument("--mobile", action="store_true",
                        help="представляться телефоном (iPhone Safari). Нужно, когда\n"
                             "работаем через мобильный прокси: десктопный браузер\n"
                             "с сотового адреса выглядит неестественно")
    parser.add_argument("--impersonate", default="",
                        help="слать запросы с TLS-отпечатком браузера через "
                             "curl_cffi, например chrome124. Обычный HTTP-клиент "
                             "виден антиботу ещё на TLS-рукопожатии")
    parser.add_argument("--log-ip", action="store_true",
                        help="перед каждым запросом узнавать реальный IP выхода "
                             "и печатать связку сессия -> IP -> ответ Avito")
    parser.add_argument("--pace", type=float, default=0.0,
                        help="пауза перед каждым запросом в потоке, сек. "
                             "Пул мобильных IP небольшой, и частые запросы "
                             "выжигают его быстрее, чем адреса восстанавливаются")
    parser.add_argument("--direct", action="store_true",
                        help="без прокси, напрямую с этой машины")
    parser.add_argument("--cooldown", type=float, default=0.0,
                        help="минимальная пауза между запросами с ОДНОГО адреса, сек; "
                             "бережёт приватные прокси от бана")
    args = parser.parse_args()

    if not URLS_FILE.exists():
        sys.exit("Нет очереди. Сначала: python scraper.py sitemap --category avtomobili")

    all_urls = [json.loads(line)["url"]
                for line in URLS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = load_done()
    todo = [u for u in all_urls if u not in done]
    if args.limit is not None:
        todo = todo[:args.limit]
    if not todo:
        print("Всё из очереди уже обработано.")
        return

    # Три взаимоисключающих режима, в порядке приоритета:
    #   1. прокси из переменных окружения (мобильный, со сменой сессии)
    #   2. --direct: напрямую с этой машины
    #   3. пул адресов: свои из файла и/или публичные списки
    import os

    rotator = None
    ring = None
    mine: list = []
    servers: list = []
    # докачивать публичные списки на ходу можно только если пул из них и
    # состоит: подмешать их к выданным провайдером мобильным адресам —
    # значит утопить хороший пул в мусоре
    from_public_lists = False

    if args.impersonate and not HAVE_CFFI:
        sys.exit("Для --impersonate нужен curl_cffi:  pip install curl_cffi")

    if args.impersonate:
        known = cffi_profiles()
        if known and args.impersonate not in known:
            mobile_ones = [p for p in known
                           if any(k in str(p).lower() for k in ("ios", "android"))]
            print(f"Профиль {args.impersonate!r} эта версия curl_cffi не знает.")
            print("Доступные мобильные профили:")
            for name in mobile_ones or known[:20]:
                print(f"  {name}")
            sys.exit(1)

    list_url = (args.proxy_list_url
                or os.environ.get("AVITO_PROXY_LIST_URL", "")).strip()
    env_server = os.environ.get("AVITO_PROXY_SERVER", "").strip()
    env_user = os.environ.get("AVITO_PROXY_USERNAME", "").strip()

    if list_url and not args.direct:
        print("[1/2] качаю список прокси по ссылке провайдера")
        try:
            mine = proxy_pool.load_from_url(list_url)
        except Exception as exc:
            sys.exit(f"Не смог скачать список: {type(exc).__name__}: {exc}")
        if not mine:
            sys.exit("По ссылке не нашлось ни одного адреса — проверьте формат.")
        servers = list(mine)
        print(f"      получено адресов: {len(servers)}")
        ring = ProxyRing(servers, priority=None, cooldown=args.cooldown,
                         strikes=args.strikes)
        from_public_lists = False
    elif env_server and env_user and not args.direct:
        rotator = RotatingSession(env_server, env_user,
                                  os.environ.get("AVITO_PROXY_PASSWORD", ""))
        if rotator.rotatable:
            print("[1/2] прокси из переменных окружения, новая сессия (то есть "
                  "новый IP) на каждый запрос")
        else:
            print("[1/2] прокси из переменных окружения; ротация недоступна — "
                  "в логине нет session-...")
    elif args.direct:
        print("[1/2] без прокси, напрямую с этой машины")
    else:
        mine = proxy_pool.load_my_proxies()
        if mine:
            print(f"[1/2] свои прокси из {proxy_pool.MY_PROXIES_FILE.name}: {len(mine)}")
            if args.only_mine:
                servers = list(mine)
                print("      публичные списки не трогаю (--only-mine)")
            else:
                print("      добираю публичные списки следом")
                servers = mine + proxy_pool.harvest()
        else:
            print("[1/2] качаю списки прокси")
            servers = proxy_pool.harvest()

        if not servers:
            sys.exit("Не удалось получить ни одного прокси.")

        if not HAVE_SOCKS:
            socks_count = sum(1 for s in servers if s.startswith("socks"))
            servers = [s for s in servers if not s.startswith("socks")]
            print(f"      ВНИМАНИЕ: PySocks не установлен, поэтому {socks_count} "
                  f"SOCKS-адресов пропущены.\n"
                  f"      А это как раз самые полезные для нас: обычные HTTP-прокси\n"
                  f"      часто не умеют HTTPS, без которого Avito не открыть.\n"
                  f"      Поставьте:  pip install PySocks\n")

        from_public_lists = not (mine and args.only_mine)
        ring = ProxyRing(servers, priority=mine or None, cooldown=args.cooldown,
                         strikes=args.strikes)
        kinds: dict = {}
        for server in servers:
            kinds[server.split("://")[0]] = kinds.get(server.split("://")[0], 0) + 1
        breakdown = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        print(f"      адресов в пуле: {ring.alive} ({breakdown})\n")


    print(f"[2/2] собираю {len(todo)} карточек в {args.workers} потоков "
          f"(готово ранее: {len(done)})\n")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tasks = queue.Queue()
    for url in todo:
        tasks.put(url)
    results = queue.Queue()
    stop = threading.Event()

    # причины отказов: без них непонятно, кто виноват — мёртвые прокси,
    # фаервол Avito или неумение прокси в HTTPS
    reasons: dict = {}
    reasons_lock = threading.Lock()

    def note(reason: str) -> None:
        with reasons_lock:
            reasons[reason] = reasons.get(reason, 0) + 1

    def worker() -> None:
        while not stop.is_set():
            try:
                url = tasks.get_nowait()
            except queue.Empty:
                return
            for _ in range(args.attempts):
                if stop.is_set():
                    return
                if rotator is not None:
                    server = rotator.make()
                elif ring is None:
                    server = None
                else:
                    server = ring.take()
                    if server is None:
                        results.put((url, None, "прокси кончились"))
                        break
                if args.pace:
                    time.sleep(args.pace)
                ip = exit_ip(server) if args.log_ip else ""
                body, error = fetch(server, url, args.timeout, args.head_bytes,
                                    args.impersonate, args.mobile)
                if args.log_ip:
                    session = server.split('session-')[-1].split(':')[0][:12]
                    print(f"      сессия {session} -> {ip} -> "
                          f"{'ОК' if body else error}")
                    note_ip(ip, bool(body))
                if body:
                    if ring is not None:
                        ring.reward(server)
                    results.put((url, body, ""))
                    break
                note(error)
                if rotator is not None:
                    continue       # просто берём следующий IP
                if ring is None:
                    break          # свой IP менять не на что
                ring.punish(server)
            else:
                results.put((url, None, "не вышло ни через один прокси"))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    for thread in threads:
        thread.start()

    next_id = load_counter()
    write_header = not OUTPUT_CSV.exists()
    ok_count = fail_count = 0
    started = time.time()

    try:
        with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as handle, \
                DONE_FILE.open("a", encoding="utf-8") as done_handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()

            for number in range(1, len(todo) + 1):
                while True:
                    try:
                        url, body, error = results.get(timeout=5)
                        break
                    except queue.Empty:
                        if not any(t.is_alive() for t in threads):
                            raise KeyboardInterrupt
                        print(f"      ... живых прокси {ring.alive if ring else 0}, "
                              f"собрано {ok_count}, неудач {fail_count}")

                if body:
                    listing = parse_html(body, url, next_id)
                    writer.writerow(asdict(listing))
                    handle.flush()
                    next_id += 1
                    ok_count += 1
                    save_counter(next_id)
                    print(f"  [{number}/{len(todo)}] {listing.title[:55]} | "
                          f"{listing.price or '—'} | {listing.city or '—'}")
                else:
                    fail_count += 1
                    if number % 10 == 0 or "кончились" in (error or ""):
                        print(f"  [{number}/{len(todo)}] мимо ({error}), "
                              f"живых прокси {ring.alive if ring else 0}")

                done_handle.write(url + "\n")
                done_handle.flush()

                # пул истощился — докачиваем свежие списки на ходу
                if ring is not None and ring.alive < args.workers:
                    # пул истощился: у провайдера перезапрашиваем его же
                    # список (он отдаёт свежую выборку), публичные списки
                    # трогаем только если пул изначально из них
                    try:
                        fresh = (proxy_pool.load_from_url(list_url) if list_url
                                 else proxy_pool.harvest() if from_public_lists
                                 else [])
                    except Exception as exc:
                        print(f"      [пул] обновить не вышло: {type(exc).__name__}")
                        fresh = []
                    if fresh:
                        added = ring.refill(fresh)
                        print(f"      [пул] добавлено свежих адресов: {added} "
                              f"(живых {ring.alive})")
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        stop.set()

    elapsed = time.time() - started
    speed = ok_count / elapsed * 60 if elapsed else 0
    print(f"\nСобрано: {ok_count}, неудач: {fail_count}, за {elapsed / 60:.1f} мин "
          f"({speed:.0f} карточек/мин)")

    # расход трафика — ключевая цифра, когда прокси с квотой:
    # по ней сразу видно, хватит ли пакета на весь тираж
    if bytes_downloaded:
        mb = bytes_downloaded / 1024 / 1024
        print(f"Трафик: {mb:.1f} МБ всего", end="")
        if ok_count:
            per = bytes_downloaded / ok_count / 1024
            total_gb = per * 30000 / 1024 / 1024
            print(f", {per:.0f} КБ на карточку -> "
                  f"на 30000 нужно ~{total_gb:.2f} ГБ")
        else:
            print()

    if ip_stats:
        print(f"\nАдреса выхода: {len(ip_stats)} различных на "
              f"{sum(v['ok'] + v['fail'] for v in ip_stats.values())} запросов")
        for ip, stat in sorted(ip_stats.items(), key=lambda kv: -kv[1]["ok"]):
            mark = "ПРОПУСКАЕТ" if stat["ok"] else "блокирует "
            print(f"  {mark}  {ip:<45} ок {stat['ok']}, отказов {stat['fail']}")
        if len(ip_stats) < 5:
            print("\n  Пул адресов крошечный. Ротация сессии их просто перебирает")
            print("  по кругу, поэтому и не помогает: восстановиться они не успевают.")

    if reasons:
        total_attempts = sum(reasons.values())
        print(f"\nПочему не вышло ({total_attempts} попыток через прокси):")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {count:>5}  {reason}")

        # Важно не общее число отказов, а доля тех, кто ДОШЁЛ до Avito и
        # был им отвергнут: именно она показывает, есть ли смысл перебирать
        # адреса дальше. Ответ сервера (HTTP ...) означает, что связь была.
        local_errors = reasons.get("ImpersonateError", 0)
        if local_errors:
            print(f"\n  ВНИМАНИЕ: {local_errors} попыток вообще не ушли в сеть — "
                  "curl_cffi не принял имя профиля.\n"
                  "  Это ошибка настройки, а не блокировка.")
        reached = sum(c for r, c in reasons.items() if r.startswith("HTTP ")
                      or r in ("фаервол", "без данных"))
        blocked = sum(c for r, c in reasons.items()
                      if r in ("фаервол", "HTTP 429", "HTTP 439", "HTTP 403"))
        unreachable = total_attempts - reached

        print(f"\n  до Avito не дошли: {unreachable} "
              f"({unreachable / total_attempts * 100:.0f}%) — мёртвые адреса")
        print(f"  дошли до Avito:    {reached} "
              f"({reached / total_attempts * 100:.0f}%), из них заблокировано {blocked}")

        if reached and blocked == reached and ok_count == 0:
            if rotator is not None:
                print("\nВЫВОД: все запросы дошли до Avito, но он заблокировал каждый.")
                print("Адреса этого прокси сейчас под блокировкой. Мобильные IP")
                print("восстанавливаются со временем — попробуйте через несколько")
                print("часов и с паузой побольше (--pace 10 и выше).")
            else:
                print("\nВЫВОД: до Avito доходят единицы, и он блокирует ВСЕХ до одного.")
                print("Перебирать публичные списки дальше бессмысленно — их адреса")
                print("у Avito давно в чёрном списке. Нужен приватный/резидентский IP.")
        elif not reached:
            print("\nВЫВОД: ни один адрес не дошёл до Avito — списки нерабочие "
                  "или сервер не выпускает трафик на их порты.")
    if speed > 0:
        print(f"При такой скорости 30000 займут ~{30000 / speed / 60:.1f} ч")
    print(f"CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
