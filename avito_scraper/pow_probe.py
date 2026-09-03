#!/usr/bin/env python3
"""
Proof-of-work вместо капчи: что именно просит Avito.

Ответ 439 с заголовком Accept: application/json содержит не страницу
блокировки, а задачу:

    {"pow_challenge": "D9j+oc+kJtDxtw00liy3fTOvU47h1XBbGXRM42s3..."}

Это proof-of-work — «потрать процессорное время и докажи, что ты не
бот». Такое решается кодом, бесплатно и без человека, в отличие от
капчи, за которую надо платить.

Скрипт делает две вещи:
  1) забирает задачу целиком — нужны все поля, а не первые 200 байт:
     сложность, алгоритм, куда слать ответ;
  2) ищет в скриптах Avito код, который её решает. Страница грузит
     fingerprint.js, background-check.js и mon-check.js — судя по
     названиям, как раз они этим и заняты.

    python pow_probe.py
    python pow_probe.py --grep pow
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.avito.ru"
TARGET = "/web/1/js/items?categoryId=9&locationId=637640&countOnly=1"

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
JSON_HEADERS = {"User-Agent": DESKTOP_UA, "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE + "/moskva/avtomobili"}

SCRIPT_RE = re.compile(r'src=["\'](https://[^"\']+\.js)["\']', re.IGNORECASE)

# Слова, вокруг которых стоит смотреть код решателя.
WORDS = ["pow_challenge", "powChallenge", "difficulty", "leadingZero",
         "hashcash", "nonce", "x-pow", "X-Pow", "proofOfWork"]


def fetch(url: str, headers: dict, timeout: int = 40):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return response.status, dict(response.headers), raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        if exc.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return exc.code, dict(exc.headers), raw


def show_challenge() -> None:
    status, headers, raw = fetch(BASE + TARGET, JSON_HEADERS)
    print(f"ответ {status}, {len(raw)} байт\n")

    interesting = {k: v for k, v in headers.items()
                   if re.search(r"pow|challenge|captcha|firewall|retry|x-",
                                k, re.IGNORECASE)}
    print("заголовки ответа, похожие на относящиеся к делу:")
    for key, value in (interesting or {"—": "нет таких"}).items():
        print(f"  {key}: {value[:120]}")

    text = raw.decode("utf-8", "replace")
    print("\nтело целиком:")
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2)[:3000])
    except Exception:
        print(text[:3000])


def grep_scripts(page_path: str, words: list) -> None:
    page = Path(page_path).read_text(encoding="utf-8", errors="replace")
    scripts = list(dict.fromkeys(SCRIPT_RE.findall(page)))
    print(f"\nскриптов: {len(scripts)}; ищу {', '.join(words)}\n")
    for url in scripts:
        name = url.rsplit("/", 1)[-1]
        try:
            status, _, raw = fetch(url, {"User-Agent": DESKTOP_UA,
                                         "Accept-Encoding": "gzip"})
            body = raw.decode("utf-8", "replace")
        except Exception as exc:
            print(f"  {name[:46]}: {type(exc).__name__}")
            continue
        hits = {w: len(re.findall(re.escape(w), body)) for w in words}
        hits = {w: c for w, c in hits.items() if c}
        if not hits:
            continue
        print(f"=== {name} ({len(body) // 1024} КБ): "
              f"{', '.join(f'{w}×{c}' for w, c in hits.items())}")
        first = min(re.search(re.escape(w), body).start() for w in hits)
        print(body[max(0, first - 600):first + 1400])
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page", default="data/empty_200.html")
    parser.add_argument("--grep", default="",
                        help="искать это слово вместо стандартного набора")
    parser.add_argument("--only-scripts", action="store_true")
    args = parser.parse_args()

    if not args.only_scripts:
        show_challenge()
    grep_scripts(args.page, [args.grep] if args.grep else WORDS)


if __name__ == "__main__":
    main()
