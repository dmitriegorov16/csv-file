#!/usr/bin/env python3
"""
Наблюдение за сбором: сколько собрано, с какой скоростью, когда кончится.

Прогон на 30000 идёт часами, и смотреть в бегущие строки лога бесполезно:
там видно последнюю страницу, а не то, как идут дела. Здесь наоборот —
только итоговая картина, зато честная: скорость считается по реальному
приросту между замерами, а не по среднему за всё время, поэтому
замедление видно сразу.

    python status.py                 # разовый отчёт
    python status.py --watch 60      # обновлять раз в минуту
    TELEGRAM_TOKEN=... TELEGRAM_CHAT=... python status.py --watch 300 --telegram

Телеграм подключается переменными окружения; без них скрипт просто
печатает в терминал и ничего никуда не отправляет.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from scraper import CSV_FIELDS, DATA_DIR

CSV_PATH = DATA_DIR / "avito.csv"
PROGRESS = DATA_DIR / "catalog_done.txt"
TARGET = 30000


def running() -> bool:
    """Идёт ли сбор прямо сейчас — иначе цифры можно понять неверно."""
    try:
        result = subprocess.run(["pgrep", "-f", "catalog_scrape.py"],
                                capture_output=True, text=True, timeout=10)
        return bool(result.stdout.strip())
    except Exception:
        return False


def snapshot(path: Path) -> dict:
    """Пересчитывать весь CSV каждый раз накладно, но на 30000 строк это
    доли секунды, зато цифры настоящие, а не накопленные в памяти."""
    if not path.exists():
        return {"строк": 0, "полей": {}, "ссылок": 0}

    filled = {name: 0 for name in CSV_FIELDS}
    urls = set()
    rows = 0
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                urls.add(row.get("url", ""))
                for name in CSV_FIELDS:
                    if (row.get(name) or "").strip():
                        filled[name] += 1
    except Exception:
        # Файл читается прямо во время записи, и последняя строка может
        # оказаться недописанной. Это не повод падать: считаем то, что
        # успели прочитать.
        pass
    return {"строк": rows, "полей": filled, "ссылок": len(urls)}


def human_time(seconds: float) -> str:
    if seconds <= 0 or seconds > 400 * 3600:
        return "—"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def report(state: dict, previous: dict, gap: float) -> str:
    rows = state["строк"]
    lines = [f"Собрано: {rows} из {TARGET} ({rows / TARGET * 100:.1f}%)"]

    duplicates = rows - state["ссылок"]
    if duplicates:
        lines.append(f"Повторов по ссылке: {duplicates} — это ошибка, "
                     f"прогресс не читается")

    if previous and gap > 0:
        added = rows - previous["строк"]
        speed = added / gap * 60
        lines.append(f"Скорость: {speed:.0f} строк/мин (+{added} за "
                     f"{gap / 60:.1f} мин)")
        if speed > 0:
            lines.append(f"Осталось: {human_time((TARGET - rows) / speed * 60)}")
        else:
            lines.append("Прироста нет — сбор встал или закончил")

    # Пустые колонки важнее заполненных: по ним видно, что ломается.
    empty = [f"{name} {rows - count}"
             for name, count in state["полей"].items()
             if rows and count < rows]
    lines.append("Пустых значений: " + (", ".join(empty) if empty else "нет"))
    lines.append("Процесс: " + ("идёт" if running() else "не запущен"))
    return "\n".join(lines)


def send_telegram(text: str) -> str:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT", "").strip()
    if not token or not chat:
        return "TELEGRAM_TOKEN/TELEGRAM_CHAT не заданы"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as response:
            json.loads(response.read().decode("utf-8", "replace"))
        return "отправлено"
    except Exception as exc:
        return f"не отправилось: {type(exc).__name__}"


def main() -> None:
    global TARGET

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=str(CSV_PATH))
    parser.add_argument("--watch", type=float, default=0,
                        help="повторять каждые N секунд")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--target", type=int, default=TARGET)
    args = parser.parse_args()

    TARGET = args.target
    path = Path(args.csv)

    previous, previous_time = {}, 0.0
    while True:
        now = time.time()
        state = snapshot(path)
        text = report(state, previous, now - previous_time if previous else 0)
        print(f"\n[{time.strftime('%H:%M:%S')}]\n{text}")
        if args.telegram:
            print(f"телеграм: {send_telegram(text)}")

        if not args.watch:
            return
        previous, previous_time = state, now
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
