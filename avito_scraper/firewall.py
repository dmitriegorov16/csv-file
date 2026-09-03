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
# Ключи, которые Avito отдавал на странице 429. Запасной вариант: ответ
# /get их может и не содержать, а решать чем-то надо.
DEFAULT_GEETEST_ID = "2d9c743cf7d63dbc9db578a608196bcd"
DEFAULT_HCAPTCHA_KEY = "070db171-ddb9-4c93-b7f6-d25d3c9d7e28"

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


# Как виджет называется в JS и как — в ответе сервера. Это разные вещи:
# в коде константы GEETEST_CAPTCHA, а /get отвечает {"captcha":
# {"geeTest": {"type": "geeTest"}}} — имя лежит ключом, а не значением.
WIDGET_NAMES = {
    "geetest": GEETEST, "geetest_captcha": GEETEST, "geetestcaptcha": GEETEST,
    "hcaptcha": HCAPTCHA, "h_captcha": HCAPTCHA,
    "internal": INTERNAL, "internal_captcha": INTERNAL,
    "internalcaptcha": INTERNAL,
}


def find_type(data) -> str:
    """Тип виджета из ответа /get.

    Ответ приходит в виде {"success": {"result": {"captcha": {"geeTest":
    ...}}}}, то есть имя виджета — ключ. Но полагаться на точный путь
    нельзя: он у них уже отличается от того, что написано в JS. Поэтому
    обходим дерево и принимаем название и ключом, и значением."""
    if isinstance(data, str):
        return WIDGET_NAMES.get(data.lower().replace("-", "_"), "")
    if isinstance(data, dict):
        for key, value in data.items():
            known = WIDGET_NAMES.get(key.lower().replace("-", "_"), "")
            if known:
                return known
            found = find_type(value)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = find_type(value)
            if found:
                return found
    return ""


def _find(data, key: str):
    """Значение по ключу на любой глубине."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _find(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find(value, key)
            if found is not None:
                return found
    return None


def solve(session, page_url: str, page_html: str, timeout: int = 30,
          debug: bool = False) -> tuple:
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
            # id виджета может прийти прямо в ответе /get; если нет —
            # берём из страницы, а если и там нет, то известный нам.
            captcha_id = (_find(descriptor, "captchaId")
                          or _find(descriptor, "captcha_id"))
            if not captcha_id:
                match = GEETEST_ID_RE.search(page_html)
                captcha_id = match.group(1) if match else DEFAULT_GEETEST_ID
            result = solver().geetest_v4(captcha_id=captcha_id, url=page_url)
            payload["geetestResponse"] = json.loads(result["code"])
        elif kind == HCAPTCHA:
            sitekey = _find(descriptor, "sitekey") or _find(descriptor, "siteKey")
            if not sitekey:
                match = SITEKEY_RE.search(page_html)
                sitekey = match.group(1) if match else DEFAULT_HCAPTCHA_KEY
            result = solver().hcaptcha(sitekey=sitekey, url=page_url)
            payload["hCaptchaResponse"] = result["code"]
        else:
            # Своя картинка Avito: её ещё надо достать из ответа /get,
            # а формат мы пока не видели. Не выдумываем — сообщаем.
            return False, "внутренняя капча Avito, формат неизвестен"
    except Exception as exc:
        return False, f"rucaptcha: {type(exc).__name__}: {exc}"

    if debug:
        # Что именно ушло на verify. Когда сервер отвечает "verified":
        # false, вопрос всегда один: наш формат или их проверка. Без
        # этого вывода отличить одно от другого нельзя.
        print(f"      [debug] /get ответил: {json.dumps(descriptor)[:300]}")
        print(f"      [debug] шлём на verify: {json.dumps(payload)[:600]}")

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
