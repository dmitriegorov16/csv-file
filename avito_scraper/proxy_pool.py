#!/usr/bin/env python3
"""
Автоматический поиск рабочих прокси для Avito.

Что делает:
  1) качает публичные списки прокси (несколько источников сразу);
  2) проверяет каждый ПРЯМО НА AVITO, параллельно, с короткими таймаутами;
  3) оставляет только те, с которых Avito отдаёт нормальную страницу.

Проверка отбирает не "живой прокси", а "прокси, с которого видно данные".
Это важно: адрес может прекрасно работать, но его IP у Avito уже сожжён —
такой бесполезен, и тратить на него капчу не нужно. Вердикты:

    ok      — 200 и в теле нет признаков фаервола: годится
    blocked — прокси жив, но Avito отвечает блокировкой (429/439/фаервол)
    dead    — не соединяется, таймаут, мусор в ответе

Результат пишется в data/working_proxies.txt и используется scraper.py.

    python proxy_pool.py                 # найти и сохранить рабочие
    python proxy_pool.py --needed 10     # искать, пока не наберётся 10
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
WORKING_FILE = DATA_DIR / "working_proxies.txt"

# Публичные списки. Отдаются как обычные текстовые файлы, обновляются часто.
# Публичные списки. SOCKS5 идёт первым не случайно: обычные HTTP-прокси
# часто вообще не умеют CONNECT для HTTPS, а Avito работает только по
# HTTPS — то есть такой адрес бесполезен, даже когда он жив. SOCKS
# туннелирует TCP как есть, поэтому для нашей задачи подходит лучше.
SOURCES = [
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("socks5", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
               "&protocol=socks5&proxy_format=protocolipport&format=text"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("http", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
]

# Проверяем на настоящей странице объявления: именно её нам и качать.
TEST_URL = ("https://www.avito.ru/sankt-peterburg/avtomobili/"
            "hyundai_solaris_1.6_at_2019_395_000_km_8329727762")

FIREWALL_MARKERS = ("Доступ ограничен", "js-firewall-form", "проверка безопасности")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

HOST_PORT_RE = re.compile(r"^(?:(\w+)://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


def _download(url: str, deadline: float = 45.0, max_bytes: int = 8_000_000) -> str:
    """Качает текстовый список с ЖЁСТКИМ ограничением по времени и размеру.

    Одного timeout у сокета мало: он отсчитывается заново на каждом
    полученном байте, поэтому сервер, отдающий данные по капле, держит
    соединение сколько угодно — на этом сбор и завис на 10 минут. Читаем
    кусками и сами следим за общим временем."""
    request = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    started = time.monotonic()
    chunks = []
    total = 0
    with urllib.request.urlopen(request, timeout=8) as response:
        while True:
            if time.monotonic() - started > deadline:
                raise TimeoutError(f"источник отдаёт медленнее {deadline:.0f}с")
            chunk = response.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                break
    return b"".join(chunks).decode("utf-8", "replace")


CACHE_FILE = DATA_DIR / "proxies_cache.txt"
CACHE_TTL = 900  # 15 минут: список всё равно живёт недолго


def _read_cache(ttl: float = CACHE_TTL) -> list:
    """Свежий кэш списка адресов, если он есть.

    Нужен потому, что сеть до источников может быть медленной или
    нестабильной, и перекачивать одни и те же списки на каждом запуске
    дороже, чем сам сбор."""
    if not CACHE_FILE.exists():
        return []
    age = time.time() - CACHE_FILE.stat().st_mtime
    if age > ttl:
        return []
    servers = [line.strip() for line in
               CACHE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if servers:
        print(f"  [кэш] {len(servers)} адресов, возраст {age / 60:.0f} мин "
              f"(перекачаю, когда станет старше {ttl / 60:.0f} мин)")
    return servers


def _write_cache(servers: list) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text("\n".join(servers) + "\n", encoding="utf-8")
    except Exception:
        pass


def harvest(limit: int = 3000, use_cache: bool = True) -> list[str]:
    """Скачивает публичные списки и возвращает адреса вида http://ip:port."""
    if use_cache:
        cached = _read_cache()
        if cached:
            random.shuffle(cached)
            return cached[:limit]

    found: list[str] = []
    seen: set[str] = set()

    def load_one(entry):
        scheme, url = entry
        name = f"{scheme}/{url.split('?')[0].rsplit('/', 1)[-1]}"
        try:
            return name, scheme, _download(url), None
        except Exception as exc:
            return name, scheme, "", exc

    # источники качаются параллельно: их девять, а сеть до некоторых
    # бывает медленной — последовательно это выливалось в минуты ожидания
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for name, scheme, text, error in pool.map(load_one, SOURCES):
            if error is not None:
                print(f"  [-] {name}: {type(error).__name__}")
                continue
            count = 0
            for line in text.splitlines():
                match = HOST_PORT_RE.match(line.strip())
                if not match:
                    continue
                server = f"{match.group(1) or scheme}://{match.group(2)}:{match.group(3)}"
                if server not in seen:
                    seen.add(server)
                    found.append(server)
                    count += 1
            print(f"  [+] {name}: {count}")

    if found:
        _write_cache(found)
    random.shuffle(found)
    return found[:limit]


def check_proxy(server: str, timeout: int = 12) -> tuple[str, str, str]:
    """Возвращает (server, вердикт, детали). Вердикты: ok / blocked / dead."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": server, "https": server}))
    request = urllib.request.Request(TEST_URL, headers=HEADERS)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(60000).decode("utf-8", "replace")
            if any(marker in body for marker in FIREWALL_MARKERS):
                return server, "blocked", "фаервол"
            if response.status == 200 and 'og:title' in body:
                return server, "ok", "есть og:title"
            if response.status == 200:
                return server, "blocked", "200, но без данных"
            return server, "blocked", f"status {response.status}"
    except urllib.error.HTTPError as exc:
        return server, "blocked", f"status {exc.code}"
    except Exception as exc:
        return server, "dead", type(exc).__name__


def find_working(servers: list[str], needed: int, workers: int = 60,
                 timeout: int = 12) -> list[str]:
    """Проверяет прокси пачками, пока не наберёт needed рабочих."""
    working: list[str] = []
    stats = {"ok": 0, "blocked": 0, "dead": 0}
    checked = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_proxy, s, timeout): s for s in servers}
        for future in as_completed(futures):
            checked += 1
            try:
                server, verdict, detail = future.result()
            except Exception:
                stats["dead"] += 1
                continue
            stats[verdict] += 1
            if verdict == "ok":
                working.append(server)
                print(f"  РАБОТАЕТ  {server}  ({detail})")
                if len(working) >= needed:
                    break
            if checked % 100 == 0:
                print(f"  ... проверено {checked}/{len(servers)}  "
                      f"живых={stats['ok']} заблокировано={stats['blocked']} мертво={stats['dead']}")

    print(f"\nПроверено {checked}. Рабочих: {stats['ok']}, "
          f"заблокировано Avito: {stats['blocked']}, мёртвых: {stats['dead']}")
    return working


MY_PROXIES_FILE = DATA_DIR / "my_proxies.txt"


def load_my_proxies() -> list:
    """Свои прокси из data/my_proxies.txt.

    Понимает формат выгрузки Webshare и подобных — ip:port:логин:пароль,
    а также обычный scheme://логин:пароль@host:port. Файл лежит в data/,
    которая не попадает в git, поэтому пароли не утекут в репозиторий.

    Приватные адреса всегда идут первыми: их выдали лично вам, их не
    затёрли тысячи чужих ботов, и у них принципиально другие шансы."""
    if not MY_PROXIES_FILE.exists():
        return []

    servers = []
    for line in MY_PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            servers.append(line)
            continue
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            servers.append(f"http://{user}:{password}@{host}:{port}")
        elif len(parts) == 2:
            servers.append(f"http://{parts[0]}:{parts[1]}")
    return servers


def load_from_url(url: str, timeout: int = 45) -> list:
    """Список прокси по ссылке провайдера.

    Многие сервисы отдают выданные вам адреса обычным текстовым файлом с
    фильтрами прямо в URL (тип, страна, количество). Это лучше одного
    статичного адреса: с одного IP Avito отдаёт примерно одну карточку,
    поэтому решает ширина пула, а не качество отдельного адреса.

    Формат строк бывает разный — ip:port, ip:port:логин:пароль или полный
    URL со схемой; разбираем все три."""
    text = _download(url, deadline=timeout)
    servers = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            servers.append(line)
            continue
        parts = line.split(":")
        if len(parts) == 2:
            servers.append(f"http://{parts[0]}:{parts[1]}")
        elif len(parts) == 4:
            host, port, user, password = parts
            servers.append(f"http://{user}:{password}@{host}:{port}")
    return servers


def load_working() -> list[str]:
    if not WORKING_FILE.exists():
        return []
    return [line.strip() for line in WORKING_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def save_working(servers: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKING_FILE.write_text("\n".join(servers) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--needed", type=int, default=5,
                        help="сколько рабочих прокси набрать (по умолчанию 5)")
    parser.add_argument("--limit", type=int, default=3000,
                        help="сколько адресов проверить максимум")
    parser.add_argument("--workers", type=int, default=60,
                        help="сколько проверок параллельно")
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()

    print("[1/2] качаю списки прокси")
    servers = harvest(args.limit)
    if not servers:
        sys.exit("Не удалось скачать ни одного списка — проверьте сеть.")
    print(f"      всего адресов на проверку: {len(servers)}\n")

    print(f"[2/2] проверяю на Avito (нужно {args.needed}, потоков {args.workers})")
    working = find_working(servers, args.needed, args.workers, args.timeout)

    if working:
        save_working(working)
        print(f"\nСохранено в {WORKING_FILE}:")
        for server in working:
            print(f"  {server}")
        print("\nТеперь можно запускать сбор — scraper.py их подхватит.")
    else:
        print("\nРабочих не нашлось. Списки обновляются постоянно — "
              "попробуйте запустить ещё раз или увеличить --limit.")


if __name__ == "__main__":
    main()
