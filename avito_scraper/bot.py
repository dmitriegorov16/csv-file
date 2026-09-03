#!/usr/bin/env python3
"""
Бот-пульт на aiogram: нажал «Запустить» — пошёл сбор.

Обмен с Telegram отдан библиотеке. Самописный HTTP тут не годился: на
этом сервере POST к api.telegram.org не проходит вовсе, а нерабочий IPv6
добавлял к каждому запросу двадцать секунд ожидания. aiogram разбирается
с этим сам.

Разделение обязанностей:

    сбор  — считает и пишет порции по 50 строк в data/chunks/
    бот   — замечает новые файлы и отправляет их в чат

Так весь трафик к Telegram идёт одним путём, через библиотеку, а сбор
ничего о Telegram не знает и не может из-за него встать.

    /start   начать сбор
    /status  сколько собрано и что пустует
    /stop    остановить

    TELEGRAM_TOKEN=... RUCAPTCHA_API_KEY=... python bot.py --limit 150
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

import status

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CHUNKS = DATA / "chunks"
LOG = DATA / "run.log"
CSV = DATA / "avito.csv"

HELP = ("Команды:\n"
        "/start — начать сбор\n"
        "/status — как идут дела\n"
        "/stop — остановить")

options: argparse.Namespace
sent_chunks: set = set()
watching_chat: int = 0


def collector_running() -> bool:
    result = subprocess.run(["pgrep", "-f", "catalog_scrape.py"],
                            capture_output=True, text=True)
    return bool(result.stdout.strip())


def start_collector() -> str:
    if collector_running():
        return "Сбор уже идёт. /status — посмотреть, /stop — остановить."

    DATA.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-u", str(HERE / "catalog_scrape.py"),
               "--limit", str(options.limit), "--chunk", str(options.chunk)]
    if options.proxy:
        command += ["--proxy", options.proxy]
    if options.proxy_list_url:
        command += ["--proxy-list-url", options.proxy_list_url]

    # Токен сборщику не передаём: отправка — забота бота. Ему остаётся
    # только писать файлы, и никакая заминка в Telegram его не тронет.
    environment = {k: v for k, v in os.environ.items() if k != "TELEGRAM_TOKEN"}
    with LOG.open("a", encoding="utf-8") as handle:
        subprocess.Popen(command, stdout=handle, stderr=handle, cwd=str(HERE),
                         env=environment, start_new_session=True)
    return (f"Начинаю сбор: цель {options.limit} объявлений.\n"
            f"Каждые {options.chunk} строк пришлю файлом, в конце — весь CSV.")


def stop_collector() -> str:
    if not collector_running():
        return "Сбор не запущен."
    subprocess.run(["pkill", "-f", "catalog_scrape.py"])
    return "Остановил. Прогресс сохранён — /start продолжит с того же места."


def status_text() -> str:
    status.TARGET = options.limit
    state = status.snapshot(CSV)
    if not state["строк"]:
        return ("Пока ничего не собрано.\n"
                + ("Сбор идёт, ждём первых строк." if collector_running()
                   else "Сбор не запущен, /start — начать."))
    return status.report(state, {}, 0)


async def watch_chunks(bot: Bot) -> None:
    """Отправлять порции по мере появления и итог по завершении.

    Бот смотрит на папку, а не слушает сбор: так он переживает и
    перезапуск сбора, и собственный перезапуск — уже отправленное помечено
    в памяти, а всё новое просто окажется на диске."""
    was_running = False
    while True:
        await asyncio.sleep(5)
        if not watching_chat:
            continue

        if CHUNKS.exists():
            for path in sorted(CHUNKS.glob("*.csv")):
                if path.name in sent_chunks:
                    continue
                sent_chunks.add(path.name)
                try:
                    rows = max(0, len(path.read_text(encoding="utf-8")
                                      .splitlines()) - 1)
                    await bot.send_document(
                        watching_chat, FSInputFile(path),
                        caption=f"{path.stem}: {rows} строк")
                except Exception as exc:
                    await bot.send_message(
                        watching_chat,
                        f"Порция {path.name} готова, но не отправилась "
                        f"({type(exc).__name__}). Она на сервере: {path}")

        running = collector_running()
        if was_running and not running:
            # сбор только что закончился — отдаём весь файл
            try:
                await bot.send_message(watching_chat, "Сбор завершён.\n"
                                       + status_text())
                if CSV.exists():
                    await bot.send_document(watching_chat, FSInputFile(CSV),
                                            caption="Итоговый CSV")
            except Exception as exc:
                await bot.send_message(
                    watching_chat,
                    f"Готово, но файл не отправился ({type(exc).__name__}). "
                    f"Он на сервере: {CSV}")
        was_running = running


def main() -> None:
    global options

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--chunk", type=int, default=50)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-list-url", default="")
    options = parser.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        sys.exit("Нужен TELEGRAM_TOKEN")
    if not os.environ.get("RUCAPTCHA_API_KEY"):
        print("RUCAPTCHA_API_KEY не задан — капчу решать будет нечем")

    bot = Bot(token=token)
    dispatcher = Dispatcher()

    @dispatcher.message(Command("start"))
    async def on_start(message: Message) -> None:
        global watching_chat
        watching_chat = message.chat.id
        await message.answer(start_collector())

    @dispatcher.message(Command("status"))
    async def on_status(message: Message) -> None:
        global watching_chat
        watching_chat = message.chat.id
        await message.answer(status_text())

    @dispatcher.message(Command("stop"))
    async def on_stop(message: Message) -> None:
        await message.answer(stop_collector())

    @dispatcher.message()
    async def on_other(message: Message) -> None:
        await message.answer(HELP)

    async def run() -> None:
        asyncio.create_task(watch_chunks(bot))
        me = await bot.get_me()
        print(f"бот @{me.username} слушает. Цель: {options.limit}, "
              f"порция: {options.chunk}")
        # накопившиеся команды не разгребаем: бот, поднятый после
        # нескольких попыток, иначе ответит на всю очередь разом
        await dispatcher.start_polling(bot, drop_pending_updates=True)

    asyncio.run(run())


if __name__ == "__main__":
    main()
