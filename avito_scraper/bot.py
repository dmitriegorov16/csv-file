#!/usr/bin/env python3
"""
Бот-пульт: нажал «Запустить» — пошёл сбор.

Держится на сервере и слушает команды. Смысл в том, чтобы не заходить по
ssh ради запуска и не держать открытый терминал: сбор идёт часами, а
телефон всегда под рукой.

    /start   начать сбор (и ответить, что начал)
    /status  сколько собрано, с какой скоростью, сколько осталось
    /stop    остановить

Сам сбор запускается отдельным процессом — он же шлёт порции по 50 строк
и итоговый файл. Так падение бота не роняет сбор, а перезапуск бота не
плодит вторую копию: перед запуском он проверяет, не идёт ли уже одна.

    TELEGRAM_TOKEN=... RUCAPTCHA_API_KEY=... python bot.py --limit 150
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import notify
import status

HERE = Path(__file__).resolve().parent
LOG = HERE / "data" / "run.log"

HELP = ("Команды:\n"
        "/start — начать сбор\n"
        "/status — как идут дела\n"
        "/stop — остановить")


def collector_running() -> bool:
    """Идёт ли сбор прямо сейчас — чтобы не запустить вторую копию."""
    try:
        result = subprocess.run(["pgrep", "-f", "catalog_scrape.py"],
                                capture_output=True, text=True, timeout=10)
        return bool(result.stdout.strip())
    except Exception:
        return False


def start_collector(limit: int, chunk: int, proxy: str = "",
                    proxy_list_url: str = "") -> str:
    if collector_running():
        return "Сбор уже идёт. /status — посмотреть, /stop — остановить."

    LOG.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(HERE / "catalog_scrape.py"),
               "--limit", str(limit), "--chunk", str(chunk), "--telegram"]
    if proxy:
        command += ["--proxy", proxy]
    if proxy_list_url:
        command += ["--proxy-list-url", proxy_list_url]
    # Процесс переживает завершение бота: сбор на 30000 идёт часами, и
    # привязывать его к жизни пульта неправильно.
    with LOG.open("a", encoding="utf-8") as handle:
        subprocess.Popen(command, stdout=handle, stderr=handle,
                         cwd=str(HERE), start_new_session=True)
    return (f"Начинаю сбор: цель {limit} объявлений.\n"
            f"Каждые {chunk} строк пришлю файлом, в конце — весь CSV.\n"
            f"/status — как идут дела.")


def stop_collector() -> str:
    if not collector_running():
        return "Сбор не запущен."
    subprocess.run(["pkill", "-f", "catalog_scrape.py"], timeout=10)
    return "Остановил. Прогресс сохранён — /start продолжит с того же места."


def status_text(target: int) -> str:
    status.TARGET = target
    state = status.snapshot(status.CSV_PATH)
    if not state["строк"]:
        return ("Пока ничего не собрано."
                + ("\nСбор идёт, ждём первых строк." if collector_running()
                   else "\nСбор не запущен, /start — начать."))
    # Скорость здесь не посчитать: замер один. Зато видно объём и пустоты.
    return status.report(state, {}, 0)


def handle(text: str, args) -> str:
    command = text.strip().split()[0].lower() if text.strip() else ""
    if command in ("/start", "start", "старт"):
        return start_collector(args.limit, args.chunk,
                               getattr(args, "proxy", ""),
                               getattr(args, "proxy_list_url", ""))
    if command in ("/status", "status", "статус"):
        return status_text(args.limit)
    if command in ("/stop", "stop", "стоп"):
        return stop_collector()
    return HELP


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--chunk", type=int, default=50)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-list-url", default="",
                        help="сбор пойдёт через адрес из списка провайдера")
    args = parser.parse_args()

    # Вывод в файл Python буферизует, и лог фонового бота остаётся пустым
    # ровно тогда, когда он нужнее всего — когда что-то пошло не так.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    if not notify.enabled():
        sys.exit("Нужен TELEGRAM_TOKEN")
    if not os.environ.get("RUCAPTCHA_API_KEY"):
        print("RUCAPTCHA_API_KEY не задан — капчу решать будет нечем\n")

    me = notify._call("getMe", {})
    username = me.get("result", {}).get("username", "?")
    print(f"бот @{username} слушает. Цель: {args.limit}, порция: {args.chunk}")
    print("Отправьте ему /start в Telegram.\n")

    offset = 0
    while True:
        try:
            # длинное ожидание: сервер держит запрос, пока не придёт
            # сообщение — так не приходится опрашивать в цикле каждую секунду
            updates = notify._call("getUpdates",
                                   {"offset": offset, "timeout": 25},
                                   timeout=40)
        except Exception as exc:
            print(f"getUpdates: {type(exc).__name__}, жду и повторю")
            time.sleep(5)
            continue

        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = message.get("text", "")
            chat = str((message.get("chat") or {}).get("id", ""))
            if not chat:
                continue
            notify._chat_cache = chat        # отвечаем тому, кто написал
            print(f"  {chat}: {text[:60]}")
            answer = handle(text, args)
            notify.send_message(answer)


if __name__ == "__main__":
    main()
