#!/usr/bin/env python3
"""
Прохождение капчи Avito прямым HTTP, без браузера.

Разбор сохранённой страницы 429 показал, что весь обмен — два JSON-запроса:

    POST /web/5/firewallCaptcha/get     {"refreshInternalCaptcha": false}
        -> какой виджет показывать: INTERNAL_CAPTCHA / H_CAPTCHA / GEETEST_CAPTCHA
    POST /web/3/firewallCaptcha/verify  {captcha, hCaptchaResponse, geetestResponse}
        -> {"verified": true} и кука captcha_solved

Браузер для этого не нужен: страница сама ходит обычным fetch. Смысл
здесь не в экономии памяти, а в том, что 429 — это не отказ, а
приглашение: IP до Avito дошёл. Решив капчу один раз, мы получаем
сессию с кукой и гоним через этот же адрес много карточек подряд.
Именно поэтому одна решённая капча стоит не одной страницы, а десятков.

Ключ rucaptcha берётся из RUCAPTCHA_API_KEY. Без ключа модуль честно
говорит, что решать нечем, и ничего не делает.
"""

from __future__ import annotations

import json
import os
import re
import threading

ORIGIN = "https://www.avito.ru"
GET_URL = ORIGIN + "/web/5/firewallCaptcha/get"
VERIFY_URL = ORIGIN + "/web/3/firewallCaptcha/verify"

GEETEST = "GEETEST_CAPTCHA"
HCAPTCHA = "H_CAPTCHA"
INTERNAL = "INTERNAL_CAPTCHA"

# Ключи виджетов зашиты в страницу; вытаскиваем их оттуда, а не из
# констант — при смене ключа код не должен ломаться молча.
SITEKEY_RE = re.compile(r'data-sitekey=["\']([0-9a-f-]{20,})["\']', re.IGNORECASE)
GEETEST_ID_RE = re.compile(r'captchaId\s*[:=]\s*["\']([0-9a-f]{20,})["\']', re.IGNORECASE)

_solver = None
_solver_lock = threading.Lock()

# Статистика: сколько решено, сколько потрачено впустую. Без неё нельзя
# сказать, окупается ли затея.
stats = {"попыток": 0, "решено": 0, "не подтвердилось": 0, "нет виджета": 0}
stats_lock = threading.Lock()


def _note(key: str) -> None:
    with stats_lock:
        stats[key] = stats.get(key, 0) + 1


def available() -> bool:
    return bool(os.environ.get("RUCAPTCHA_API_KEY", "").strip())


def solver():
    """Клиент rucaptcha создаётся один на процесс и лениво."""
    global _solver
    with _solver_lock:
        if _solver is None:
            from twocaptcha import TwoCaptcha
            _solver = TwoCaptcha(os.environ["RUCAPTCHA_API_KEY"].strip(),
                                 defaultTimeout=180, pollingInterval=5)
        return _solver


def find_type(data) -> str:
    """Тип виджета из ответа /get.

    Точную форму ответа мы не знаем — на странице она проходит через
    normalizeCaptcha(). Поэтому ищем известное значение где угодно внутри,
    вместо того чтобы угадывать имя поля и падать на первом же ответе."""
    if isinstance(data, str):
        return data if data in (GEETEST, HCAPTCHA, INTERNAL) else ""
    if isinstance(data, dict):
        for value in data.values():
            found = find_type(value)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = find_type(value)
            if found:
                return found
    return ""


def solve(session, page_url: str, page_html: str, timeout: int = 30) -> tuple:
    """Пройти капчу в этой сессии. Возвращает (получилось, что случилось).

    session — requests.Session с уже настроенным прокси: капчу надо
    решать ровно для того IP, которому её показали, иначе смысла нет."""
    if not available():
        return False, "нет RUCAPTCHA_API_KEY"

    _note("попыток")
    headers = {"Content-Type": "application/json", "Referer": page_url,
               "Origin": ORIGIN, "X-Requested-With": "XMLHttpRequest"}

    try:
        response = session.post(GET_URL, json={"refreshInternalCaptcha": False},
                                headers=headers, timeout=timeout)
        descriptor = response.json()
    except Exception as exc:
        return False, f"/get не ответил: {type(exc).__name__}"

    kind = find_type(descriptor)
    if not kind:
        _note("нет виджета")
        return False, f"неизвестный виджет: {json.dumps(descriptor)[:200]}"

    payload = {"captcha": "", "hCaptchaResponse": "", "geetestResponse": {}}
    try:
        if kind == GEETEST:
            match = GEETEST_ID_RE.search(page_html)
            if not match:
                return False, "на странице нет captchaId для geetest"
            result = solver().geetest_v4(captcha_id=match.group(1), url=page_url)
            payload["geetestResponse"] = json.loads(result["code"])
        elif kind == HCAPTCHA:
            match = SITEKEY_RE.search(page_html)
            if not match:
                return False, "на странице нет sitekey для hcaptcha"
            result = solver().hcaptcha(sitekey=match.group(1), url=page_url)
            payload["hCaptchaResponse"] = result["code"]
        else:
            # Своя картинка Avito: её ещё надо достать из ответа /get,
            # а формат мы пока не видели. Не выдумываем — сообщаем.
            return False, "внутренняя капча Avito, формат неизвестен"
    except Exception as exc:
        return False, f"rucaptcha: {type(exc).__name__}: {exc}"

    # X-Cube страница считает кодом на WebGL. Проверяет ли его сервер —
    # неизвестно, поэтому сначала пробуем без него: если дело в нём, это
    # будет видно по ответу, и мы будем знать, а не предполагать.
    try:
        response = session.post(VERIFY_URL, json=payload, headers=headers,
                                timeout=timeout)
        verdict = response.json()
    except Exception as exc:
        return False, f"/verify не ответил: {type(exc).__name__}"

    if verdict.get("verified") is True:
        _note("решено")
        session.cookies.set("captcha_solved", "1", domain="www.avito.ru", path="/")
        return True, "капча пройдена"

    _note("не подтвердилось")
    return False, f"verify отказал: {json.dumps(verdict)[:200]}"


def report() -> str:
    with stats_lock:
        if not stats["попыток"]:
            return ""
        parts = ", ".join(f"{name} {count}" for name, count in stats.items() if count)
        return f"капча: {parts}"
