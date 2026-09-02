#!/usr/bin/env python3
"""
Диагностика одной карточки объявления: что реально приходит в HTML.

Без ретраев, без решения капчи, без длинных ожиданий — просто открыть
страницу через тот же браузер/прокси, что и scraper.py, и честно показать:

  * HTTP-статус
  * заголовок страницы и есть ли форма фаервола
  * извлекаются ли нужные поля (title/price/description/image)

Это отвечает на вопрос, ради которого всё затевалось: страница объявления
отдаёт данные или нет. HTML сохраняется в item_debug.html для разбора.

    python check_item.py                       # первая ссылка из очереди
    python check_item.py https://www.avito.ru/...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from scraper import (URLS_FILE, launch_browser, new_context, attach_diagnostics,
                     extract_json_ld, text_or_empty, attr_or_empty)

OUT_FILE = Path(__file__).resolve().parent / "item_debug.html"


def pick_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    if not URLS_FILE.exists():
        sys.exit("Очередь пуста. Сначала: python scraper.py sitemap --category avtomobili --max-urls 100")
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return json.loads(line)["url"]
    sys.exit("Очередь пуста.")


def main() -> None:
    url = pick_url()
    print(f"[check] {url}\n")

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        context = new_context(browser, block_resources=True)
        page = context.new_page()
        attach_diagnostics(page)

        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"\nHTTP статус документа : {response.status if response else 'нет ответа'}")

        # даём SSR-разметке/гидрации немного времени, но без долгих ожиданий
        page.wait_for_timeout(4000)

        title = page.title()
        firewall = page.query_selector(".js-firewall-form") is not None
        print(f"Заголовок страницы    : {title!r}")
        print(f"Форма фаервола на стр.: {firewall}")

        json_ld = extract_json_ld(page)
        fields = {
            "og:title": attr_or_empty(page, 'meta[property="og:title"]', "content"),
            "og:image": attr_or_empty(page, 'meta[property="og:image"]', "content"),
            "meta description": attr_or_empty(page, 'meta[name="description"]', "content"),
            "h1 (title-info)": text_or_empty(page, '[data-marker="item-view/title-info"]'),
            "цена (item-price)": text_or_empty(page, '[data-marker="item-view/item-price"]'),
            "описание": text_or_empty(page, '[data-marker="item-view/item-description"]'),
            "адрес": text_or_empty(page, '[data-marker="item-view/item-address"]'),
            "JSON-LD name": json_ld.get("name", ""),
        }

        print("\nИзвлечённые поля:")
        for name, value in fields.items():
            value = " ".join(str(value).split())
            shown = (value[:90] + "…") if len(value) > 90 else value
            status = "OK  " if value else "ПУСТО"
            print(f"  {status} {name:20} {shown}")

        html = page.content()
        OUT_FILE.write_text(html, encoding="utf-8")
        print(f"\nHTML сохранён: {OUT_FILE} ({len(html)} байт)")

        filled = sum(1 for v in fields.values() if v)
        print(f"\nИТОГ: заполнено полей {filled}/{len(fields)}")
        if filled == 0:
            print("Данных нет — страница либо заблокирована, либо не отрендерилась.")
        elif filled < len(fields) / 2:
            print("Часть данных есть — вероятно, надо поправить селекторы под текущую вёрстку.")
        else:
            print("Данные на месте — можно гнать scrape по всей очереди.")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
