#!/usr/bin/env python3
"""
Почему бот не работает: проверка по шагам.

«Бот не работает» — это четыре разные поломки, и лечатся они по-разному:
неверный токен, бот не знает чат, чат знает но сообщение не уходит, и
файлы не проходят при работающих сообщениях. Скрипт проверяет их по
очереди и на каждом шаге говорит, что именно делать.

    TELEGRAM_TOKEN=... python check_telegram.py
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import notify


def step(number: int, title: str) -> None:
    print(f"\n{number}) {title}")


def main() -> None:
    token = notify.token()
    step(1, "токен")
    if not token:
        print("   TELEGRAM_TOKEN пуст.")
        print("   Запускайте так: TELEGRAM_TOKEN=123:ABC python check_telegram.py")
        return
    print(f"   есть, {token[:12]}…")

    step(2, "бот отвечает на свой токен")
    try:
        with urllib.request.urlopen(
                notify.API.format(token=token, method="getMe"), timeout=20) as response:
            info = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"   не достучались до Telegram: {type(exc).__name__}")
        print("   проверьте сеть на сервере: curl https://api.telegram.org")
        return
    if not info.get("ok"):
        print(f"   Telegram отверг токен: {str(info)[:200]}")
        print("   возьмите новый у @BotFather (/mybots -> API Token)")
        return
    bot = info["result"]
    print(f"   это @{bot.get('username')} ({bot.get('first_name')})")

    step(3, "кому писать")
    explicit = os.environ.get("TELEGRAM_CHAT", "").strip()
    chat = notify.chat_id()
    if not chat:
        print("   чат неизвестен.")
        print(f"   Откройте @{bot.get('username')}, нажмите «Запустить» "
              f"или отправьте /start, потом повторите проверку.")
        print("   Telegram запрещает боту писать первым — это не наша ошибка,")
        print("   а правило: диалог начинает человек.")
        return
    print(f"   chat_id = {chat}" + (" (из TELEGRAM_CHAT)" if explicit
                                    else " (из вашего сообщения боту)"))

    step(4, "сообщение")
    print(f"   {notify.send_message('Проверка связи: сообщения доходят.')}")

    step(5, "файл")
    sample = Path("/tmp/telegram_check.csv")
    sample.write_text("id,url,title\n1,https://www.avito.ru/x_1,Пробная строка\n",
                      encoding="utf-8")
    print(f"   {notify.send_file(sample, 'Проверка связи: файлы доходят.')}")

    print("\nЕсли оба шага сказали «отправлено» — бот готов, "
          "запускайте сбор с --telegram")


if __name__ == "__main__":
    main()
