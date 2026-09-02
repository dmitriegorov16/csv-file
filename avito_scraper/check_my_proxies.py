#!/usr/bin/env python3
"""
Осторожная проверка своих (приватных) прокси на Avito.

Приватных адресов мало, и сжечь их легко: Avito банит IP именно за
частые запросы. Поэтому здесь всё нарочно медленно и по одному —
последовательно, по ОДНОМУ запросу на адрес, с паузой между ними.
Десять адресов — десять запросов, больше ничего.

Задача — только выяснить, пускает Avito с этих IP или нет. Массовый
сбор запускать имеет смысл, лишь если ответ "да".

Адреса берутся из data/my_proxies.txt (формат Webshare
ip:port:логин:пароль или обычный scheme://логин:пароль@host:port).

    python check_my_proxies.py
    python check_my_proxies.py --pause 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import proxy_pool
from scraper import URLS_FILE

try:
    import requests
except ImportError:
    sys.exit("Нужен requests:  pip install requests")


def sample_urls(count: int) -> list:
    """Разные объявления для разных прокси: один и тот же URL, запрошенный
    десять раз подряд, сам по себе выглядит подозрительно."""
    if not URLS_FILE.exists():
        return [proxy_pool.TEST_URL] * count
    urls = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            urls.append(json.loads(line)["url"])
        if len(urls) >= count:
            break
    return urls or [proxy_pool.TEST_URL] * count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", type=float, default=4.0,
                        help="пауза между запросами, секунд (по умолчанию 4)")
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()

    servers = proxy_pool.load_my_proxies()
    if not servers:
        sys.exit(f"Нет файла {proxy_pool.MY_PROXIES_FILE}.\n"
                 f"Положите туда адреса в формате ip:port:логин:пароль, по одному в строке.")

    urls = sample_urls(len(servers))
    print(f"Проверяю {len(servers)} своих прокси — по одному запросу на каждый, "
          f"пауза {args.pause}с\n")

    good, blocked, dead = [], [], []

    for number, server in enumerate(servers, 1):
        shown = server.split("@")[-1]        # без логина и пароля в выводе
        url = urls[number - 1]
        try:
            response = requests.get(url, headers=proxy_pool.HEADERS,
                                    proxies={"http": server, "https": server},
                                    timeout=args.timeout)
            status, body = response.status_code, response.text
        except Exception as exc:
            dead.append(server)
            print(f"  {number:>2}. {shown:<24} НЕ РАБОТАЕТ  {type(exc).__name__}")
            time.sleep(args.pause)
            continue

        if any(marker in body for marker in proxy_pool.FIREWALL_MARKERS):
            blocked.append(server)
            print(f"  {number:>2}. {shown:<24} БЛОКИРОВКА   Avito показал фаервол ({status})")
        elif status == 200 and "og:title" in body:
            good.append(server)
            start = body.find('property="og:title"')
            title = body[start:start + 200].split('content="')[-1].split('"')[0] if start > 0 else ""
            print(f"  {number:>2}. {shown:<24} ГОДЕН        {title[:45]}")
        else:
            blocked.append(server)
            print(f"  {number:>2}. {shown:<24} БЛОКИРОВКА   HTTP {status}")

        if number < len(servers):
            time.sleep(args.pause)

    print(f"\n{'=' * 58}")
    print(f"годных {len(good)}, заблокировано {len(blocked)}, не работают {len(dead)}")

    if good:
        print(f"\nAvito пускает с этих адресов. Можно собирать — но осторожно,\n"
              f"иначе сожжём и их. Безопасный запуск (по потоку на адрес,\n"
              f"пауза между обращениями к одному и тому же IP):\n")
        print(f"  python fast_scrape.py --only-mine --workers {len(good)} "
              f"--cooldown 6 --limit 20")
    elif blocked:
        print("\nАдреса живы, но Avito блокирует их все — как и публичные.\n"
              "Значит дело в самой природе датацентровых IP, и бесплатные\n"
              "варианты исчерпаны: нужен резидентский прокси.")
    else:
        print("\nНи один адрес не отозвался — проверьте логин/пароль и то,\n"
              "что прокси активированы в личном кабинете.")


if __name__ == "__main__":
    main()
