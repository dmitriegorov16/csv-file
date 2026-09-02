#!/usr/bin/env python3
"""
Сколько запросов выдерживает один бесплатный прокси на Avito.

Вопрос, от которого зависит вся схема: прокси умирают сами по себе
(перегруженный публичный адрес) или их сжигает Avito после первых же
запросов? Разница решающая:

    живёт ~50 запросов  -> на 30000 карточек нужно ~600 прокси, это реально
    умирает после 2-3   -> нужно 10000+ прокси, схема не работает

Скрипт берёт прокси и долбит им карточки подряд, классифицируя каждый
ответ, пока не умрёт:

    ok        — страница с данными
    ФАЕРВОЛ   — Avito ответил блокировкой: значит сжёг именно он
    прокси    — обрыв/таймаут/пустой ответ: сдох сам прокси

По финальной статистике видно, кто виноват и сколько адресов нужно.

    python proxy_endurance.py                          # первый из сохранённых
    python proxy_endurance.py http://1.2.3.4:8080 40
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

import proxy_pool
from scraper import URLS_FILE

HEADERS = proxy_pool.HEADERS
FIREWALL_MARKERS = proxy_pool.FIREWALL_MARKERS


def urls(count: int) -> list[str]:
    if not URLS_FILE.exists():
        sys.exit("Нет очереди. Сначала: python scraper.py sitemap --category avtomobili --max-urls 200")
    out = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line)["url"])
        if len(out) >= count:
            break
    return out


def fetch(server: str, url: str, timeout: int = 20) -> tuple[str, str]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": server, "https": server}))
    try:
        with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return "ФАЕРВОЛ" if exc.code in (429, 439, 403) else "прокси", f"HTTP {exc.code}"
    except Exception as exc:
        return "прокси", type(exc).__name__
    if any(m in body for m in FIREWALL_MARKERS):
        return "ФАЕРВОЛ", "страница блокировки"
    if "og:title" in body:
        return "ok", f"{len(body)} байт"
    return "прокси", "ответ без данных"


def main() -> None:
    server = sys.argv[1] if len(sys.argv) > 1 else None
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    if not server:
        saved = proxy_pool.load_working()
        if not saved:
            sys.exit("Нет сохранённых прокси. Сначала: python proxy_pool.py")
        server = saved[0]

    print(f"[тест] прокси {server}, до {total} запросов подряд\n")
    targets = urls(total)
    counts = {"ok": 0, "ФАЕРВОЛ": 0, "прокси": 0}
    first_ok_streak = 0
    streak_broken = False

    for number, url in enumerate(targets, 1):
        verdict, detail = fetch(server, url)
        counts[verdict] += 1
        if verdict == "ok" and not streak_broken:
            first_ok_streak += 1
        elif verdict != "ok":
            streak_broken = True
        print(f"  {number:>3}. {verdict:<8} {detail}")
        if counts["ФАЕРВОЛ"] >= 3 or counts["прокси"] >= 5:
            print("\n  дальше нет смысла — прокси перестал отдавать данные")
            break
        time.sleep(1)

    done = sum(counts.values())
    print(f"\nИтог по {done} запросам: успешных {counts['ok']}, "
          f"блокировок Avito {counts['ФАЕРВОЛ']}, отказов прокси {counts['прокси']}")
    print(f"Успешных подряд с начала: {first_ok_streak}")

    if counts["ФАЕРВОЛ"] > counts["прокси"]:
        print("\nВИНОВАТ AVITO: адрес сжигается лимитером.")
        if first_ok_streak:
            print(f"На один прокси приходится ~{first_ok_streak} карточек, "
                  f"на 30000 нужно ~{30000 // max(first_ok_streak, 1)} прокси.")
    elif counts["прокси"] > counts["ok"]:
        print("\nВИНОВАТ ПРОКСИ: адрес сам нестабилен, Avito тут ни при чём.")
        print("Лечится ротацией: берём следующий из пула и продолжаем.")
    else:
        print("\nПрокси держится. Можно гнать сбор через него.")


if __name__ == "__main__":
    main()
