#!/usr/bin/env python3
"""
Кто виноват: наш код или IP прокси?

Проверка построена так, чтобы ответ был однозначным. Берётся ОДНА сессия
прокси (то есть один и тот же IP) и один и тот же URL, и запрос делается
разными клиентами подряд, с небольшими паузами:

    curl          — эталон, им мы получали 200
    urllib        — стандартная библиотека
    requests      — то, чем работает fast_scrape
    curl_cffi     — с TLS-отпечатком настоящего браузера (если установлен)

Порядок клиентов меняется между кругами, чтобы преимущество не доставалось
тому, кто ходит первым: если IP «портится» после первого же обращения,
первый клиент всегда выглядел бы лучше остальных.

Читать результат так:

    все дают одинаково        -> дело в IP, код ни при чём
    curl 200, а Python 439    -> дело в нашем клиенте, чиним заголовки/TLS
    разброс внутри одного     -> дело в темпе: IP тухнет по ходу проверки
    клиента между кругами

    python compare_clients.py
    python compare_clients.py --rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TEST_URL = ("https://www.avito.ru/sankt-peterburg/avtomobili/"
            "hyundai_solaris_1.6_at_2019_395_000_km_8329727762")

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": DESKTOP_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def proxy_url(session: str) -> str:
    """Один и тот же прокси, но с фиксированной сессией — значит один IP
    на всё время проверки."""
    server = os.environ["AVITO_PROXY_SERVER"].split("://")[-1]
    user = os.environ["AVITO_PROXY_USERNAME"]
    password = os.environ.get("AVITO_PROXY_PASSWORD", "")
    import re
    user = re.sub(r"(session-)([A-Za-z0-9]+)$", r"\1" + session, user)
    return (f"http://{urllib.parse.quote(user, safe='')}:"
            f"{urllib.parse.quote(password, safe='')}@{server}")


def verdict(status, body: str) -> str:
    if isinstance(status, int) and status == 200 and "og:title" in body:
        return "200 ДАННЫЕ ЕСТЬ"
    if "Доступ ограничен" in body or "js-firewall-form" in body:
        kind = "капча" if "js-firewall-form" in body else "жёсткий блок"
        return f"{status} блокировка ({kind}, {len(body)} б)"
    return f"{status} ({len(body)} б)"


def via_curl(proxy: str) -> str:
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/tmp/cmp_curl.html", "-w", "%{http_code}",
             "--max-time", "35", "-x", proxy, "-A", DESKTOP_UA, TEST_URL],
            capture_output=True, text=True, timeout=60)
        body = open("/tmp/cmp_curl.html", encoding="utf-8", errors="replace").read()
        return verdict(result.stdout.strip(), body)
    except Exception as exc:
        return f"ошибка {type(exc).__name__}"


def via_urllib(proxy: str) -> str:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(urllib.request.Request(TEST_URL, headers=HEADERS),
                         timeout=35) as response:
            return verdict(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return verdict(exc.code, exc.read().decode("utf-8", "replace"))
        except Exception:
            return f"{exc.code} (тело не прочиталось)"
    except Exception as exc:
        return f"ошибка {type(exc).__name__}"


def via_requests(proxy: str) -> str:
    try:
        import requests
    except ImportError:
        return "requests не установлен"
    try:
        response = requests.get(TEST_URL, headers=HEADERS,
                                proxies={"http": proxy, "https": proxy}, timeout=35)
        return verdict(response.status_code, response.text)
    except Exception as exc:
        return f"ошибка {type(exc).__name__}"


def via_cffi(proxy: str) -> str:
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        return "curl_cffi не установлен"
    for profile in ("chrome124", "chrome120", "chrome110"):
        try:
            response = cffi.get(TEST_URL, impersonate=profile,
                                proxies={"http": proxy, "https": proxy}, timeout=35)
            return f"[{profile}] " + verdict(response.status_code, response.text)
        except Exception as exc:
            if "Impersonate" not in type(exc).__name__:
                return f"ошибка {type(exc).__name__}"
    return "ни один профиль не подошёл"


CLIENTS = [("curl", via_curl), ("urllib", via_urllib),
           ("requests", via_requests), ("curl_cffi", via_cffi)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--pause", type=float, default=6.0)
    args = parser.parse_args()

    if not os.environ.get("AVITO_PROXY_SERVER"):
        sys.exit("Нужны AVITO_PROXY_SERVER / AVITO_PROXY_USERNAME / AVITO_PROXY_PASSWORD")

    session = f"{random.randrange(16 ** 12):012x}"
    proxy = proxy_url(session)
    print(f"Сессия {session} — все запросы уйдут с одного IP\n")

    # какой именно IP достался — иначе выводы не с чем связывать
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open("https://ipinfo.io/json", timeout=25) as response:
            info = json.loads(response.read().decode("utf-8", "replace"))
        print(f"IP: {info.get('ip')}  ({info.get('org')})\n")
    except Exception as exc:
        print(f"IP определить не вышло: {type(exc).__name__}\n")

    results: dict = {}
    for round_number in range(1, args.rounds + 1):
        order = list(CLIENTS)
        if round_number % 2 == 0:
            order.reverse()          # чтобы очередь не давала преимущества
        print(f"--- круг {round_number}")
        for name, func in order:
            outcome = func(proxy)
            results.setdefault(name, []).append(outcome)
            print(f"  {name:<10} {outcome}")
            time.sleep(args.pause)

    print("\n" + "=" * 58)
    for name, outcomes in results.items():
        print(f"  {name:<10} {' | '.join(outcomes)}")

    flat = [o for outs in results.values() for o in outs]
    got_data = [name for name, outs in results.items()
                if any("ДАННЫЕ ЕСТЬ" in o for o in outs)]
    print()
    if got_data:
        print(f"ВЫВОД: данные получили — {', '.join(got_data)}.")
        losers = [n for n in results if n not in got_data]
        if losers:
            print(f"А {', '.join(losers)} на том же IP не смогли — значит дело в клиенте,")
            print("и надо равняться на того, кто прошёл.")
    elif all("блокировка" in o for o in flat):
        print("ВЫВОД: заблокированы все клиенты одинаково — дело в IP, а не в коде.")
    else:
        print("ВЫВОД: картина смешанная, смотрите строки выше.")


if __name__ == "__main__":
    main()
