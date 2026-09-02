#!/usr/bin/env python3
"""
Разведка: какие внутренние API-запросы делает сам сайт Avito.

Зачем: у Avito есть внутреннее JSON-API (вида m.avito.ru/api/N/items?key=...).
Ключ `key` зашит во фронтенд и меняется при обновлениях, поэтому брать его из
чужих статей бесполезно — он протухает. Этот скрипт открывает страницу в
Playwright и логирует все запросы, похожие на API, вместе с полным URL,
методом и заголовками. Из этого видно актуальный эндпоинт, ключ и набор
параметров.

Если API-запросы найдутся — дальше сбор можно делать обычными HTTP-запросами
(httpx/requests) без браузера: это в разы дешевле по трафику (не тянем
картинки/CSS/JS-бандлы) и быстрее.

Запуск (прокси и капча — как в scraper.py, через те же переменные окружения):

    python sniff_api.py                                    # категория по умолчанию
    python sniff_api.py https://www.avito.ru/moskva/avtomobili
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import captcha_solver
from scraper import launch_browser, new_context, try_unblock

DEFAULT_URL = "https://www.avito.ru/moskva/transport"
OUT_FILE = Path(__file__).resolve().parent / "api_requests.json"

# что считаем "похожим на API" — служебные запросы аналитики/рекламы
# отсекаем, чтобы не тонуть в шуме
API_HINTS = ("/api/", "/web/", "graphql", "/items", "/catalog")
NOISE_HINTS = ("analytics", "metrika", "google", "criteo", "adfox", "sentry",
               "doubleclick", "mc.yandex", "stat", "banner")


def looks_like_api(url: str) -> bool:
    low = url.lower()
    if any(n in low for n in NOISE_HINTS):
        return False
    return any(h in low for h in API_HINTS)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    captured: list[dict] = []

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        # ресурсы НЕ блокируем: нам важно увидеть всё, что грузит страница
        context = new_context(browser, block_resources=False)
        page = context.new_page()

        def on_request(request):
            if not looks_like_api(request.url):
                return
            captured.append({
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
            })
            print(f"[api] {request.method} {request.url[:160]}")

        page.on("request", on_request)

        print(f"[sniff] открываю {target}")
        page.goto(target, wait_until="domcontentloaded", timeout=30000)

        # если прилетела капча/блокировка — пробуем пройти, дальше сайт
        # начнёт грузить настоящие данные, а нам именно они и нужны
        try_unblock(page, (2.0, 4.0))

        # даём странице время догрузить данные объявлений
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        page.wait_for_timeout(5000)

        context.close()
        browser.close()

    OUT_FILE.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[sniff] похожих на API запросов: {len(captured)}")
    print(f"[sniff] полный список сохранён в {OUT_FILE}")

    keys = set()
    for item in captured:
        if "key=" in item["url"]:
            keys.add(item["url"].split("key=")[1].split("&")[0])
    if keys:
        print(f"[sniff] найденные значения key: {', '.join(sorted(keys))}")


if __name__ == "__main__":
    main()
