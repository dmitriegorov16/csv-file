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
import urllib.request
from dataclasses import asdict
from typing import Optional

import proxy_pool
from scraper import (CSV_FIELDS, DATA_DIR, OUTPUT_CSV, URLS_FILE, Listing,
                     capitalize_city)

DONE_FILE = DATA_DIR / "done.txt"          # append-only: по строке на ссылку
COUNTER_FILE = DATA_DIR / "counter.json"   # только следующий id

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

FIREWALL_MARKERS = ("Доступ ограничен", "js-firewall-form", "проверка безопасности")

META_RE_CACHE: dict = {}
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# Разбор HTML: чистые функции, тестируются без сети (см. test_parse.py)
# --------------------------------------------------------------------------

def meta_content(page_html: str, attr: str, value: str) -> str:
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
    """Весь текст элемента с data-marker, включая вложенные теги.

    Наивное "до первого закрывающего тега" тут не годится: описание
    состоит из нескольких абзацев, и всё после первого </p> терялось бы."""
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
            return strip_tags(page_html[start:next_close.start()])
        position = next_close.end()
    return strip_tags(page_html[start:limit])


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


def parse_html(page_html: str, url: str, item_id: int) -> Listing:
    json_ld = extract_json_ld(page_html)
    offers = json_ld.get("offers") if isinstance(json_ld.get("offers"), dict) else {}

    title = (meta_content(page_html, "property", "og:title")
             or block_text(page_html, "item-view/title-info")
             or json_ld.get("name", ""))
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

    crumbs = [strip_tags(b) for b in
              re.findall(r'itemprop=["\']name["\'][^>]*>([^<]{1,80})<', page_html)]
    crumbs = [c for c in crumbs if c and c != title]
    category = crumbs[-1] if crumbs else ""
    if not category and isinstance(json_ld.get("category"), str):
        category = json_ld["category"]

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
    def __init__(self, servers: list, strikes: int = 2):
        self.servers = list(servers)
        random.shuffle(self.servers)
        self.strikes = strikes
        self.failures: dict = {}
        self.lock = threading.Lock()
        self.position = 0

    def take(self) -> Optional[str]:
        with self.lock:
            if not self.servers:
                return None
            self.position = (self.position + 1) % len(self.servers)
            return self.servers[self.position]

    def punish(self, server: str) -> None:
        with self.lock:
            self.failures[server] = self.failures.get(server, 0) + 1
            if self.failures[server] >= self.strikes and server in self.servers:
                self.servers.remove(server)
                self.failures.pop(server, None)

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
            return len(self.servers)


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


def _proxy_dict(server: str) -> dict:
    # socks5h означает "резолвить DNS на стороне прокси" — так надёжнее
    # и не светит наружу, какие домены мы запрашиваем
    if server.startswith("socks5://"):
        server = "socks5h://" + server[len("socks5://"):]
    return {"http": server, "https": server}


def fetch(server: str, url: str, timeout: int):
    """Возвращает (html, '') либо ('', причина).

    SOCKS-прокси качаются через requests+PySocks: urllib их не умеет
    вообще, а именно они для нас важнее всего — обычные HTTP-прокси часто
    не поддерживают CONNECT, без которого HTTPS (а Avito только по HTTPS)
    невозможен в принципе."""
    if server.startswith("socks") and not HAVE_REQUESTS:
        return "", "нет requests для socks"

    try:
        if HAVE_REQUESTS:
            response = requests.get(url, headers=HEADERS, proxies=_proxy_dict(server),
                                    timeout=timeout, allow_redirects=True)
            if response.status_code != 200:
                return "", f"HTTP {response.status_code}"
            body = response.text
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": server, "https": server}))
            with opener.open(urllib.request.Request(url, headers=HEADERS),
                             timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return "", f"HTTP {exc.code}"
    except Exception as exc:
        return "", type(exc).__name__
    if any(marker in body for marker in FIREWALL_MARKERS):
        return "", "фаервол"
    if "og:title" not in body:
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

    print("[1/2] качаю списки прокси")
    servers = proxy_pool.harvest()
    if not servers:
        sys.exit("Не удалось скачать списки прокси.")

    if not HAVE_SOCKS:
        socks_count = sum(1 for s in servers if s.startswith("socks"))
        servers = [s for s in servers if not s.startswith("socks")]
        print(f"      ВНИМАНИЕ: PySocks не установлен, поэтому {socks_count} "
              f"SOCKS-адресов пропущены.\n"
              f"      А это как раз самые полезные для нас: обычные HTTP-прокси\n"
              f"      часто не умеют HTTPS, без которого Avito не открыть.\n"
              f"      Поставьте:  pip install PySocks\n")

    ring = ProxyRing(servers)
    kinds = {}
    for server in servers:
        kind = server.split("://")[0]
        kinds[kind] = kinds.get(kind, 0) + 1
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
                server = ring.take()
                if server is None:
                    results.put((url, None, "прокси кончились"))
                    break
                body, error = fetch(server, url, args.timeout)
                if body:
                    ring.reward(server)
                    results.put((url, body, ""))
                    break
                note(error)
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
                        print(f"      ... живых прокси {ring.alive}, "
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
                              f"живых прокси {ring.alive}")

                done_handle.write(url + "\n")
                done_handle.flush()

                # пул истощился — докачиваем свежие списки на ходу
                if ring.alive < args.workers:
                    fresh = proxy_pool.harvest()
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

    if reasons:
        total_attempts = sum(reasons.values())
        print(f"\nПочему не вышло ({total_attempts} попыток через прокси):")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {count:>5}  {reason}")

        # Важно не общее число отказов, а доля тех, кто ДОШЁЛ до Avito и
        # был им отвергнут: именно она показывает, есть ли смысл перебирать
        # адреса дальше. Ответ сервера (HTTP ...) означает, что связь была.
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
