#!/usr/bin/env python3
"""
Что именно просит страница блокировки Avito.

Капчу мы умеем решать через браузер, но браузер стоит памяти, времени и
трафика. Чтобы повторить тот же обмен голым HTTP, надо знать три вещи:
какой виджет показан, с какими параметрами и куда отправляется решение.
Всё это лежит в самой странице — скрипт вытаскивает только это, не
заставляя читать 27 КБ минифицированного JS глазами.

    python inspect_block.py data/firewall_429.html
    python inspect_block.py data/firewall_403.html
"""

from __future__ import annotations

import html as html_module
import re
import sys
from pathlib import Path


def show(label: str, values, limit: int = 12) -> None:
    values = [v for v in dict.fromkeys(values) if v]
    print(f"\n{label} ({len(values)}):")
    if not values:
        print("  —")
        return
    for value in values[:limit]:
        text = " ".join(html_module.unescape(str(value)).split())
        print(f"  {text[:160]}")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/firewall_429.html")
    if not path.exists():
        sys.exit(f"Нет файла {path}")
    page = path.read_text(encoding="utf-8", errors="replace")
    print(f"{path}: {len(page)} символов")

    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.DOTALL | re.IGNORECASE)
    print(f"заголовок: {' '.join(title.group(1).split()) if title else '—'}")

    show("подключённые скрипты", re.findall(r'<script[^>]+src=["\']([^"\']+)', page))
    show("виджеты капчи (sitekey/captchaId/gt)",
         re.findall(r'(?:data-sitekey|captchaId|captcha_id|["\']gt["\']\s*:)'
                    r'\s*[=:]?\s*["\']?([A-Za-z0-9_\-]{8,})', page))
    show("формы", re.findall(r'<form[^>]*>', page))
    show("поля формы", re.findall(r'<input[^>]*>', page))
    show("константы вида CAPTCHA/TYPE", re.findall(r'\b[A-Z][A-Z_0-9]{3,30}\b', page), 25)

    # Куда уходит решение: относительные и абсолютные адреса из JS.
    endpoints = re.findall(r'["\'](/[a-z0-9_\-/]{4,60})["\']', page, re.IGNORECASE)
    endpoints = [e for e in endpoints
                 if not re.search(r"\.(js|css|png|jpe?g|svg|woff2?|ico)$", e, re.I)]
    show("возможные адреса запросов", endpoints, 20)

    show("имена функций проверки",
         re.findall(r'\b(?:function\s+)?(\w*[Cc]aptcha\w*)\s*[=(]', page), 20)
    show("имена кук", re.findall(r'\b([a-z_]{4,30})=[^;"\']{0,40};\s*(?:Max-Age|path|expires)',
                                 page, re.IGNORECASE))

    # Куски JS вокруг ключевых слов — чтобы увидеть сам обмен.
    for keyword in ("verifyCaptcha", "fetch(", "XMLHttpRequest", "location.href",
                    "Max-Age"):
        for match in list(re.finditer(re.escape(keyword), page))[:2]:
            start = max(0, match.start() - 120)
            fragment = " ".join(page[start:match.end() + 220].split())
            print(f"\n… {keyword}:\n  {fragment[:340]}")


if __name__ == "__main__":
    main()
