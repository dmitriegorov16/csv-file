#!/usr/bin/env python3
"""
Версия CSV, которая открывается везде.

В описаниях объявлений есть переносы строк, и по стандарту это законно:
поле в кавычках может занимать несколько физических строк. Но многие
просмотрщики, редакторы и импортёры этого не умеют и обрывают таблицу на
первом же таком месте — файл на 37000 записей показывается как 10000.

Здесь переносы внутри полей заменяются пробелами. Текст остаётся целым,
просто в одну строку, зато запись занимает ровно одну строку файла и
открывается чем угодно, включая Excel и Google Таблицы.

    python flatten.py                       # data/avito.csv -> data/avito_flat.csv
    python flatten.py --out ~/готовый.csv
    python flatten.py --bom                 # для старого Excel под Windows
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from scraper import CSV_FIELDS, DATA_DIR


def flatten(value: str) -> str:
    """Переносы и табуляции — в пробелы, лишние пробелы схлопнуть."""
    return " ".join(str(value or "").split())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", default=str(DATA_DIR / "avito.csv"))
    parser.add_argument("--out", default=str(DATA_DIR / "avito_flat.csv"))
    parser.add_argument("--bom", action="store_true",
                        help="добавить метку кодировки: старый Excel под "
                             "Windows без неё показывает кириллицу кракозябрами")
    args = parser.parse_args()

    source = Path(args.src)
    if not source.exists():
        sys.exit(f"Нет файла {source}")

    # Описания бывают длинными, стандартный предел разбора их не вмещает
    csv.field_size_limit(10 ** 7)

    rows = multiline = 0
    encoding = "utf-8-sig" if args.bom else "utf-8"
    with source.open(encoding="utf-8", newline="") as src, \
            open(args.out, "w", newline="", encoding=encoding) as dst:
        writer = csv.DictWriter(dst, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in csv.DictReader(src):
            if any("\n" in (row.get(name) or "") for name in CSV_FIELDS):
                multiline += 1
            writer.writerow({name: flatten(row.get(name)) for name in CSV_FIELDS})
            rows += 1

    print(f"записей: {rows}")
    print(f"из них с переносами внутри полей: {multiline} "
          f"— именно они и обрывали таблицу")
    print(f"готово: {args.out}")


if __name__ == "__main__":
    main()
