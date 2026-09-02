#!/usr/bin/env python3
"""
Кто виноват: прокси или Avito?

Берёт пачку адресов из публичных списков и гоняет каждый по двум целям:

    1) нейтральный HTTPS-сайт  — работает ли прокси вообще и умеет ли HTTPS
    2) карточка Avito          — пускает ли Avito с этого IP

Из сочетания результатов сразу виден диагноз:

    оба провалились          -> адрес мёртв, Avito ни при чём
    нейтральный ок, Avito нет -> прокси живой, но IP у Avito сожжён
    оба ок                   -> адрес годится для сбора

Публичные HTTP-прокси часто вообще не умеют CONNECT для HTTPS, а Avito
доступен только по HTTPS — поэтому первую цель проверять обязательно,
иначе несостоятельность списка легко перепутать с блокировкой.

    python proxy_probe.py              # 200 адресов
    python proxy_probe.py --count 500
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import proxy_pool

NEUTRAL_URL = "https://example.com/"
AVITO_URL = proxy_pool.TEST_URL
HEADERS = proxy_pool.HEADERS


def try_url(server: str, url: str, timeout: int) -> tuple[bool, str]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": server, "https": server}))
    try:
        with opener.open(urllib.request.Request(url, headers=HEADERS),
                         timeout=timeout) as response:
            body = response.read(60000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, type(exc).__name__
    if any(marker in body for marker in proxy_pool.FIREWALL_MARKERS):
        return False, "фаервол"
    return True, "ok"


def probe(server: str, timeout: int) -> tuple[str, bool, str, bool, str]:
    neutral_ok, neutral_why = try_url(server, NEUTRAL_URL, timeout)
    if not neutral_ok:
        return server, False, neutral_why, False, "-"
    avito_ok, avito_why = try_url(server, AVITO_URL, timeout)
    return server, True, neutral_why, avito_ok, avito_why


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=200, help="сколько адресов проверить")
    parser.add_argument("--workers", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()

    print("качаю списки прокси")
    servers = proxy_pool.harvest(limit=args.count)
    print(f"проверяю {len(servers)} адресов: сначала {NEUTRAL_URL}, потом Avito\n")

    dead = alive_blocked = alive_ok = 0
    neutral_reasons: dict = {}
    avito_reasons: dict = {}
    good: list = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(probe, s, args.timeout) for s in servers]
        for done, future in enumerate(as_completed(futures), 1):
            try:
                server, neutral_ok, neutral_why, avito_ok, avito_why = future.result()
            except Exception:
                dead += 1
                continue

            if not neutral_ok:
                dead += 1
                neutral_reasons[neutral_why] = neutral_reasons.get(neutral_why, 0) + 1
            elif avito_ok:
                alive_ok += 1
                good.append(server)
                print(f"  ГОДЕН  {server}")
            else:
                alive_blocked += 1
                avito_reasons[avito_why] = avito_reasons.get(avito_why, 0) + 1

            if done % 50 == 0:
                print(f"  ... {done}/{len(servers)}  мертво={dead} "
                      f"живых_но_блок={alive_blocked} годных={alive_ok}")

    total = dead + alive_blocked + alive_ok
    print(f"\n{'=' * 55}")
    print(f"Проверено адресов: {total}")
    print(f"  мёртвых (не работают вообще):     {dead}")
    print(f"  живых, но Avito не пускает:       {alive_blocked}")
    print(f"  ГОДНЫХ (Avito отдаёт данные):     {alive_ok}")

    if neutral_reasons:
        print("\nПочему мертвы (топ причин):")
        for reason, count in sorted(neutral_reasons.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  {count:>5}  {reason}")
    if avito_reasons:
        print("\nЧем отвечает Avito живым прокси:")
        for reason, count in sorted(avito_reasons.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  {count:>5}  {reason}")

    alive = alive_ok + alive_blocked
    print(f"\n{'=' * 55}\nВЫВОД:")
    if alive == 0:
        print("Ни один адрес не работает даже на нейтральном сайте.")
        print("Значит дело не в Avito — публичные списки в принципе нерабочие")
        print("(или ваш сервер не выпускает трафик на их порты).")
    elif alive_ok == 0:
        print(f"Живых прокси {alive}, но Avito не пустил ни одного.")
        print("Значит Avito блокирует все публичные адреса — бесплатные не помогут,")
        print("нужен резидентский (DataImpulse: $5 за 5 ГБ, хватит на весь объём).")
    else:
        share = alive_ok / total * 100
        print(f"Годных {alive_ok} из {total} ({share:.1f}%).")
        print(f"На 30000 карточек при таком проценте нужно перебрать "
              f"~{int(30000 / max(alive_ok, 1) * total / 30):,} адресов — "
              f"это реально, сбор имеет смысл запускать.")
        print("\nГодные адреса:")
        for server in good[:20]:
            print(f"  {server}")


if __name__ == "__main__":
    main()
