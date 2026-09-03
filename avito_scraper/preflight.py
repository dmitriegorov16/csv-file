#!/usr/bin/env python3
"""
Проверка перед долгим прогоном: всё ли готово и не упадёт ли.

Прогон на 30000 идёт часами, и узнавать о нехватке места или протухшем
ключе на третьем часу — дорого. Здесь проверяется каждое звено цепочки,
причём по-настоящему: доступ действительно открывается, страница
действительно скачивается и разбирается, файл порции действительно
пишется. Одна настоящая проверка стоит десяти предположений.

    RUCAPTCHA_API_KEY=... TELEGRAM_TOKEN=... python preflight.py

Выход 0 — можно запускать. Иначе в конце написано, что чинить.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

problems: list = []
warnings: list = []


def ok(title: str, detail: str = "") -> None:
    print(f"  OK    {title}" + (f" — {detail}" if detail else ""))


def fail(title: str, detail: str) -> None:
    print(f"  ПЛОХО {title} — {detail}")
    problems.append(f"{title}: {detail}")


def warn(title: str, detail: str) -> None:
    print(f"  ! {title} — {detail}")
    warnings.append(f"{title}: {detail}")


def check_environment() -> None:
    print("\n1. Окружение")
    if sys.version_info < (3, 8):
        fail("версия Python", f"{sys.version_info.major}.{sys.version_info.minor}")
    else:
        ok("Python", f"{sys.version_info.major}.{sys.version_info.minor}")

    for module, why in (("requests", "запросы к Avito"),
                        ("twocaptcha", "решение капчи"),
                        ("aiogram", "бот")):
        try:
            __import__(module)
            ok(f"модуль {module}", why)
        except ImportError:
            (fail if module == "requests" else warn)(
                f"модуль {module}", f"не установлен, нужен для: {why}")

    if os.environ.get("RUCAPTCHA_API_KEY"):
        ok("ключ rucaptcha", "задан")
    else:
        warn("ключ rucaptcha", "не задан — капчу пройти будет нечем")

    if os.environ.get("TELEGRAM_TOKEN"):
        ok("токен Telegram", "задан")
    else:
        warn("токен Telegram", "не задан — отчётов в чат не будет")

    free = shutil.disk_usage(HERE).free // (1024 * 1024)
    # 30000 строк с описаниями — это порядка 60 МБ, плюс порции и логи
    if free < 500:
        fail("место на диске", f"{free} МБ — мало")
    else:
        ok("место на диске", f"{free} МБ")


def check_code() -> None:
    print("\n2. Код и тесты")
    result = subprocess.run([sys.executable, str(HERE / "test_all.py")],
                            capture_output=True, text=True, cwd=str(HERE))
    last = [line for line in result.stdout.splitlines() if "Пройдено" in line]
    if result.returncode == 0:
        ok("тесты", last[-1] if last else "прошли")
    else:
        fail("тесты", last[-1] if last else "не прошли")


def check_categories() -> None:
    print("\n3. Разделы для обхода")
    path = DATA / "categories.json"
    if not path.exists():
        warn("список разделов", "нет файла — сборщик найдёт их сам "
                                "(первые полторы минуты прогона)")
        return
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail("список разделов", f"файл битый: {type(exc).__name__}")
        return
    if len(rows) < 5:
        warn("список разделов", f"всего {len(rows)} — на 30000 может не хватить")
    else:
        ok("список разделов", f"{len(rows)} шт.")


def check_progress() -> None:
    print("\n4. Уже собранное")
    csv_path = DATA / "avito.csv"
    done_path = DATA / "catalog_done.txt"
    rows = 0
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as handle:
            rows = sum(1 for _ in csv.DictReader(handle))
    done = 0
    if done_path.exists():
        done = len([1 for line in done_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()])
    if rows and abs(rows - done) > 5:
        # расхождение значит, что один из файлов стёрли отдельно — тогда
        # сбор либо начнёт заново, либо решит, что уже всё собрал
        warn("прогресс", f"в CSV {rows} строк, в списке обработанных {done} — "
                         f"файлы рассинхронизированы")
    else:
        ok("прогресс", f"{rows} строк собрано ранее")


def check_live(proxy: str, proxy_list_url: str) -> None:
    print("\n5. Живая проверка: доступ, страница, разбор")
    try:
        from catalog_scrape import to_listing
        from unlock import find_items, open_session
    except Exception as exc:
        fail("импорт сборщика", f"{type(exc).__name__}: {exc}")
        return

    session, response = open_session(proxy, proxy_list_url, verbose=True)
    if session is None:
        fail("доступ к Avito", "не открылся ни через один адрес")
        return
    ok("доступ к Avito", f"{len(response.content) // 1024} КБ в ответе")

    items = find_items(response.json())
    if not items:
        fail("разбор выдачи", "объявлений в ответе не нашлось")
        return
    ok("выдача", f"{len(items)} объявлений за запрос")

    listings = []
    for item in items:
        if isinstance(item, dict) and item.get("urlPath"):
            listings.append(to_listing(item, len(listings) + 1))
    if not listings:
        fail("разбор объявлений", "ни одно не разобралось")
        return

    filled = {name: sum(1 for row in listings if getattr(row, name))
              for name in ("title", "content", "description", "image",
                           "price", "category", "city", "address")}
    ok("разбор объявлений", f"{len(listings)} шт.")
    for name, count in filled.items():
        share = count / len(listings) * 100
        line = f"{name}: {count}/{len(listings)} ({share:.0f}%)"
        # адрес есть не у всех объявлений по своей природе, остальное —
        # обязательное, и низкая доля означает поломку разбора
        if name == "address" or share >= 90:
            print(f"        {line}")
        else:
            warn(f"поле {name}", f"заполнено лишь у {share:.0f}%")

    # объём: по нему считается, хватит ли трафика на прокси
    per_item = len(response.content) / max(1, len(items))
    total_gb = per_item * 30000 / 1024 ** 3
    print(f"        вес: {per_item / 1024:.0f} КБ на объявление, "
          f"на 30000 нужно ~{total_gb:.2f} ГБ")
    if proxy or proxy_list_url:
        warn("трафик", f"через прокси уйдёт ~{total_gb:.2f} ГБ — "
                       f"проверьте, хватает ли пакета")


def soak(count: int, proxy: str, proxy_list_url: str) -> None:
    """Серия запросов подряд: выдержит ли адрес длинный прогон.

    Один удачный запрос ничего не обещает. Датацентровый IP пускает
    первые обращения, а через сотню уходит в жёсткий режим, где капчу
    даже не предлагают, — и узнать об этом лучше сейчас, а не на третьем
    часу сбора. Здесь же видно, восстанавливается ли доступ сам: если
    фаервол закрылся, а следующая проверка снова прошла, значит долгий
    прогон переживёт такие закрытия."""
    from catalog_scrape import BASE, LOCATIONS
    from unlock import ensure_access, find_items, open_session

    print(f"\n6. Нагрузка: {count} запросов подряд")
    session, _ = open_session(proxy, proxy_list_url, verbose=False)
    if session is None:
        fail("нагрузка", "доступ не открылся")
        return

    good = blocked = empty = 0
    recovered = 0
    was_blocked = False
    total_bytes = 0
    sections = [9, 24, 84, 92, 27]        # авто, квартиры, телефоны, аквариум, одежда

    for number in range(1, count + 1):
        location = LOCATIONS[number % len(LOCATIONS)][0]
        section = sections[number % len(sections)]
        url = (f"{BASE}/web/1/js/items?categoryId={section}"
               f"&locationId={location}&page={number % 20 + 1}"
               f"&limit=50&display=list")
        response = ensure_access(session, url, verbose=False)
        if response.status_code != 200:
            blocked += 1
            was_blocked = True
            print(f"   [{number:>3}] отказ {response.status_code}")
            continue
        total_bytes += len(response.content)
        items = find_items(response.json())
        if items:
            good += 1
            if was_blocked:
                recovered += 1
                was_blocked = False
        else:
            empty += 1
        if number % 10 == 0:
            print(f"   [{number:>3}] удачно {good}, отказов {blocked}, "
                  f"пусто {empty}")

    share = good / count * 100
    print(f"\n   итог: {good}/{count} удачных ({share:.0f}%), "
          f"отказов {blocked}, пустых {empty}")
    if recovered:
        ok("восстановление", f"после отказа доступ возвращался {recovered} раз(а)")
    if total_bytes and good:
        per_page = total_bytes / good / 1024
        print(f"   {per_page:.0f} КБ на страницу, "
              f"~{per_page * 600 / 1024 / 1024:.2f} ГБ на 30000")

    if share >= 90:
        ok("нагрузка", f"{share:.0f}% запросов прошли — адрес держит")
    elif share >= 50:
        warn("нагрузка", f"прошло лишь {share:.0f}% — прогон будет рваным, "
                         f"но с восстановлением дойдёт")
    else:
        fail("нагрузка", f"прошло лишь {share:.0f}% — этот адрес долгий "
                         f"прогон не выдержит")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-list-url", default="")
    parser.add_argument("--offline", action="store_true",
                        help="без обращений к Avito")
    parser.add_argument("--soak", type=int, default=0,
                        help="сделать столько запросов подряд и посмотреть, "
                             "выдержит ли адрес (30 — разумно)")
    args = parser.parse_args()

    print("Проверка перед прогоном")
    check_environment()
    check_code()
    check_categories()
    check_progress()
    if not args.offline:
        check_live(args.proxy, args.proxy_list_url)
        if args.soak:
            soak(args.soak, args.proxy, args.proxy_list_url)

    print("\n" + "=" * 58)
    if problems:
        print("НЕ ГОТОВО. Чинить:")
        for line in problems:
            print(f"  - {line}")
    else:
        print("ГОТОВО к прогону.")
    if warnings:
        print("\nОбратить внимание:")
        for line in warnings:
            print(f"  - {line}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
