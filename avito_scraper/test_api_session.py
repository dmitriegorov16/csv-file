#!/usr/bin/env python3
"""
Проверка схемы "один браузер — много дешёвых JSON-запросов".

Идея. Браузер умеет проходить фаервол Avito (капчу мы решаем). Страницы
объявлений при этом рендерить дорого и медленно. Но у Avito есть
внутреннее API (/web/1/items/<id> — существование подтверждено ответами
роутера), и дёрнуть его можно ПРЯМО ИЗ УЖЕ ОТКРЫТОЙ ВКЛАДКИ через
fetch(). Тогда запрос уходит с куками этой сессии и с отпечатком
настоящего браузера, а в ответ приходит JSON — без рендера, без
картинок, без бандлов.

Если схема работает, весь сбор выглядит так: один раз прошли проверку —
дальше тысячи дешёвых fetch-запросов по ID из sitemap.

    python test_api_session.py            # первые 3 ссылки из очереди
    python test_api_session.py 8329727762
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from scraper import (URLS_FILE, launch_browser, new_context, is_blocked,
                     try_unblock, human_delay)

# страница, на которой "приземляемся" и проходим проверку; дальше все
# запросы идут уже из её контекста
LANDING_URL = "https://www.avito.ru/rossiya"

ITEM_ID_RE = re.compile(r"_(\d+)$")

# формы эндпоинта, которые роутер Avito признал существующими
ENDPOINT_TEMPLATES = [
    "/web/1/items/{id}",
    "/web/1/items/{id}/card",
    "/web/2/items/{id}",
]


def item_ids_from_queue(limit: int = 3) -> list[str]:
    if len(sys.argv) > 1:
        return [sys.argv[1]]
    if not URLS_FILE.exists():
        sys.exit("Очередь пуста. Сначала: python scraper.py sitemap --category avtomobili --max-urls 100")
    ids = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = ITEM_ID_RE.search(json.loads(line)["url"])
        if match:
            ids.append(match.group(1))
        if len(ids) >= limit:
            break
    return ids


def fetch_in_page(page, path: str) -> dict:
    """Выполняет fetch прямо во вкладке — с её куками и отпечатком."""
    return page.evaluate(
        """async (path) => {
            try {
                const response = await fetch(path, {
                    headers: {'Accept': 'application/json, text/plain, */*'},
                    credentials: 'include',
                });
                const text = await response.text();
                return {status: response.status, len: text.length, body: text.slice(0, 3000)};
            } catch (error) {
                return {status: -1, len: 0, body: String(error)};
            }
        }""",
        path,
    )


def main() -> None:
    item_ids = item_ids_from_queue()
    print(f"[test] ID для проверки: {', '.join(item_ids)}")

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        context = new_context(browser, block_resources=True)
        page = context.new_page()

        print(f"[test] открываю {LANDING_URL} и прохожу проверку, если попросят")
        page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=30000)

        if is_blocked(page):
            print(f"  фаервол: {page.title()!r} — пробую пройти")
            if not try_unblock(page, (2.0, 4.0)):
                print("  ПРОЙТИ НЕ УДАЛОСЬ — дальше смысла нет, нужен другой IP")
                context.close(); browser.close()
                return
            print("  проверка пройдена")
        else:
            print(f"  проверки не потребовалось: {page.title()!r}")

        print("\n[test] дёргаю API изнутри вкладки:\n")
        good = []
        for item_id in item_ids:
            for template in ENDPOINT_TEMPLATES:
                path = template.format(id=item_id)
                result = fetch_in_page(page, path)
                status, length = result["status"], result["len"]
                print(f"  {status:>5}  {path}  ({length} байт)")
                if status == 200 and length > 50:
                    good.append((path, result["body"]))
                    preview = " ".join(result["body"][:300].split())
                    print(f"         {preview}")
                human_delay(0.5, 1.5)

        if good:
            path, body = good[0]
            Path("api_sample.json").write_text(body, encoding="utf-8")
            print(f"\nРАБОТАЕТ: {len(good)} успешных ответов. Пример сохранён в api_sample.json")
            print("Значит сбор можно делать так: одна проверка на сессию + дешёвые fetch по ID.")
            try:
                data = json.loads(body)
                print("\nКлючи верхнего уровня в JSON:", list(data)[:20])
            except Exception:
                print("(ответ не разобрался как JSON — посмотрим глазами в api_sample.json)")
        else:
            print("\nНи один эндпоинт не отдал данные. Тогда остаётся HTML карточки — "
                  "проверим, отдаётся ли она в этой же сессии.")
            result = fetch_in_page(page, f"/web/1/items/{item_ids[0]}")
            print(f"  контрольный ответ: status={result['status']} "
                  f"body={' '.join(result['body'][:200].split())}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
