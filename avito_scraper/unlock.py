#!/usr/bin/env python3
"""
Проход через фаервол Avito: капча и proof-of-work в одной цепочке.

Проверка оказалась двухступенчатой, и это выяснилось только опытом:

    403 "доступ ограничен"  -> решаем капчу -> режим меняется на 439
    439 pow_challenge       -> считаем proof-of-work -> данные

То есть капча не открывает доступ сама по себе: она переводит адрес из
жёсткого режима в тот, где сервер вообще готов разговаривать. А
разговор — это уже задача на процессор, бесплатная.

Поэтому здесь цикл, а не два шага подряд: после каждой пройденной
проверки сервер может предложить следующую, и правильно реагировать на
то, что он ответил, а не на то, что мы ожидали.

    RUCAPTCHA_API_KEY=... python unlock.py
    RUCAPTCHA_API_KEY=... python unlock.py --url https://www.avito.ru/...
"""

from __future__ import annotations

import argparse
import json
import os
import random

import requests

import firewall
import firewall_pow

CATALOG = ("https://www.avito.ru/web/1/js/items"
           "?categoryId=9&locationId=637640&page=1&limit=50&display=list")

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def build_session(proxy: str = "", proxy_list_url: str = "", verbose: bool = True):
    """Сессия, при необходимости через прокси.

    Режим, в котором с нами разговаривает Avito, определяется репутацией
    адреса: датацентровый IP после нескольких тысяч запросов уходит в
    жёсткий режим, где капчу даже не предлагают, а мобильный начинает с
    обычной проверки. Поэтому возможность сменить адрес нужна каждому
    сборщику, а не только пробным скриптам."""
    session = requests.Session()
    if not proxy and proxy_list_url:
        import proxy_pool
        addresses = proxy_pool.load_from_url(proxy_list_url)
        if not addresses:
            raise SystemExit("по ссылке провайдера не нашлось ни одного адреса")
        proxy = random.choice(addresses)
        if verbose:
            print(f"прокси из списка: {proxy}")
    if proxy:
        if "://" not in proxy:
            proxy = "http://" + proxy
        session.proxies.update({"http": proxy, "https": proxy})
        if verbose:
            try:
                info = session.get("https://ipinfo.io/json", timeout=20).json()
                print(f"выхожу с {info.get('ip')} ({info.get('org')})")
            except Exception as exc:
                print(f"IP определить не вышло: {type(exc).__name__}")
    return session


def find_items(data) -> list:
    """Список объявлений из ответа каталога, где бы он ни лежал.

    Брать data["items"] с верхнего уровня оказалось неверно: ответ
    вложенный, и такой запрос молча даёт пустоту — из-за чего живой
    доступ выглядел как «разделов не существует». Ищем по всему дереву:
    структура ответа у Avito уже менялась и поменяется снова."""
    if isinstance(data, dict):
        found = data.get("items")
        if isinstance(found, list):
            return found
        for value in data.values():
            found = find_items(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_items(value)
            if found:
                return found
    return []


def headers_for(url: str) -> dict:
    return {"User-Agent": DESKTOP_UA, "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.avito.ru/moskva/avtomobili"}


class NoAnswer:
    """Ответа не было: прокси мёртвая, сеть отвалилась, вышло время.

    Прикидывается ответом со статусом 0, чтобы вызывающий код разбирал
    один вид результата, а не ловил исключения на каждом запросе. Мёртвая
    прокся не должна ронять многочасовой сбор — она должна значить
    «возьми следующую»."""

    status_code = 0
    text = ""
    content = b""

    def __init__(self, reason: str = ""):
        self.reason = reason

    def json(self):
        return {}


def get(session, url: str, headers: dict, timeout: int):
    try:
        return session.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        return NoAnswer(type(exc).__name__)


def ensure_access(session, url: str, rounds: int = 4, verbose: bool = True,
                  timeout: int = 30):
    """Добиться ответа с данными, проходя проверки по мере их появления.

    Возвращает последний ответ. Сколько именно проверок попросят, заранее
    неизвестно: на разных адресах и в разное время бывает по-разному."""
    headers = headers_for(url)
    response = get(session, url, headers, timeout)
    if isinstance(response, NoAnswer):
        if verbose:
            print(f"   сеть молчит: {response.reason}")
        return response

    for step in range(1, rounds + 1):
        if response.status_code == 200:
            return response

        body = response.text
        if "pow_challenge" in body:
            what = "proof-of-work"
            solved, note = firewall_pow.solve(session, url, body, timeout)
        elif "captcha" in body or response.status_code in (403, 429, 439):
            what = "капча"
            solved, note = firewall.solve(session, url, body, timeout)
        else:
            if verbose:
                print(f"   [{step}] непонятный ответ {response.status_code}: "
                      f"{body[:120]}")
            return response

        if verbose:
            print(f"   [{step}] {what}: {note}")
        if not solved:
            return response
        response = get(session, url, headers, timeout)
        if isinstance(response, NoAnswer):
            if verbose:
                print(f"   [{step}] сеть молчит: {response.reason}")
            return response
        if verbose:
            print(f"   [{step}] после проверки: {response.status_code}, "
                  f"{len(response.content)} байт")

    return response


def open_session(proxy: str = "", proxy_list_url: str = "", attempts: int = 6,
                 probe_url: str = CATALOG, verbose: bool = True):
    """Сессия, через которую Avito реально отвечает.

    Прокси из списка бывают мертвы для конкретного направления: адрес
    отзывается, ipinfo через него работает, а до Avito трафик не идёт.
    Проверять это заранее отдельным запросом смысла нет — проверка и есть
    первый полезный запрос, поэтому пробуем открыть доступ и, если не
    вышло, берём следующий адрес."""
    for attempt in range(1, attempts + 1):
        session = build_session(proxy, proxy_list_url, verbose=verbose)
        response = ensure_access(session, probe_url, verbose=verbose)
        if response.status_code == 200:
            if verbose:
                print("доступ открыт\n")
            return session, response
        if verbose:
            reason = getattr(response, "reason", response.status_code)
            print(f"[{attempt}/{attempts}] не вышло ({reason})"
                  + (", беру следующий адрес\n" if proxy_list_url else "\n"))
        if not proxy_list_url:
            break        # менять нечего: адрес один
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=CATALOG)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--save", default="data/catalog.json")
    args = parser.parse_args()

    session = requests.Session()
    if args.proxy:
        proxy = args.proxy if "://" in args.proxy else "http://" + args.proxy
        session.proxies.update({"http": proxy, "https": proxy})

    if not os.environ.get("RUCAPTCHA_API_KEY"):
        print("RUCAPTCHA_API_KEY не задан — капчу решать нечем, "
              "пройдёт только proof-of-work\n")

    print(f"открываю: {args.url[:88]}")
    response = ensure_access(session, args.url, rounds=args.rounds)

    print(f"\nитог: {response.status_code}, {len(response.content)} байт")
    if response.status_code != 200:
        raise SystemExit(f"не открылось: {response.text[:200]}")

    data = response.json()
    with open(args.save, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    print(f"сохранено в {args.save}")

    items = firewall_pow._find(data, "items")
    if isinstance(items, list):
        print(f"объявлений в ответе: {len(items)}")
        if items and isinstance(items[0], dict):
            print(f"поля первого: {', '.join(list(items[0])[:16])}")
    else:
        print(f"ключи ответа: {', '.join(list(data)[:14])}")


if __name__ == "__main__":
    main()
