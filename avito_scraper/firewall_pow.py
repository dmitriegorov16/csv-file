#!/usr/bin/env python3
"""
Proof-of-work Avito: считаем вместо того, чтобы платить.

Протокол вычитан из bootstrap-desktop.js целиком, гадать не пришлось:

    POST /web/3/firewallPow/get     {"challenge": <pow_challenge>}
        -> success.result.challenge_jwt
    в JWT:                          {"id": ..., "compl": N}
    решение:                        SHA-256("<id>:<nonce>") должен
                                    начинаться с N нулей
    POST /web/3/firewallPow/verify  {"challenge": ..., "nonce": ...}
        -> success.result.verified

Это обычный hashcash. Отличие от капчи принципиальное: капча стоит денег
за каждое решение, а это — только процессорное время, которого у сервера
и так вдоволь. Python считает SHA-256 быстрее, чем браузер через
crypto.subtle, так что мы в лучшем положении, чем обычный посетитель.

Задача приходит только при Accept: application/json — с браузерными
заголовками тот же 439 отдаёт HTML-заглушку, в которой условия не видно.
Из-за этого мы её месяц не замечали.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time

BASE = "https://www.avito.ru"
GET_URL = BASE + "/web/3/firewallPow/get"
VERIFY_URL = BASE + "/web/3/firewallPow/verify"

JSON_ACCEPT = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

# Ограничение сверху: если задача не решилась за столько попыток, значит
# мы неверно поняли условие, и надо не крутить процессор впустую, а
# сказать об этом.
MAX_NONCE = 50_000_000

stats = {"задач": 0, "решено": 0, "не подтвердилось": 0, "перебор": 0,
         "секунд": 0.0}
stats_lock = threading.Lock()


def _note(key: str, value=1) -> None:
    with stats_lock:
        stats[key] = stats.get(key, 0) + value


def challenge_from(body: str) -> str:
    """Достать pow_challenge из ответа 439."""
    try:
        data = json.loads(body)
    except Exception:
        match = re.search(r'"pow_challenge"\s*:\s*"([^"]+)"', body)
        return match.group(1) if match else ""
    return _find(data, "pow_challenge") or ""


def _find(data, key: str):
    """Значение по ключу на любой глубине.

    Форму ответа мы знаем со слов минифицированного JS, а он может
    измениться. Искать по всему дереву надёжнее, чем полагаться на
    точный путь и падать на первом же обновлении."""
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


def jwt_payload(token: str) -> dict:
    """Полезная нагрузка JWT без проверки подписи — она нам и не нужна,
    токен мы возвращаем тому же, кто его выдал."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("это не JWT")
    raw = parts[1]
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw))


def find_nonce(task_id: str, complexity: int, limit: int = MAX_NONCE) -> int:
    """Подобрать nonce, при котором хеш начинается с нужного числа нулей."""
    prefix = "0" * complexity
    encode = f"{task_id}:%d".__mod__
    for nonce in range(limit):
        if hashlib.sha256(encode(nonce).encode()).hexdigest().startswith(prefix):
            return nonce
    raise RuntimeError(f"не подобрался за {limit} попыток")


def solve(session, page_url: str, body: str, timeout: int = 30) -> tuple:
    """Пройти проверку в этой сессии. Возвращает (получилось, что случилось)."""
    challenge = challenge_from(body)
    if not challenge:
        return False, "в ответе нет pow_challenge"

    _note("задач")
    headers = dict(JSON_ACCEPT)
    headers.update({"Content-Type": "application/json", "Referer": page_url})

    try:
        response = session.post(GET_URL, json={"challenge": challenge},
                                headers=headers, timeout=timeout)
        token = _find(response.json(), "challenge_jwt")
    except Exception as exc:
        return False, f"/get не ответил: {type(exc).__name__}"
    if not token:
        return False, f"нет challenge_jwt: {response.text[:150]}"

    try:
        payload = jwt_payload(token)
        task_id, complexity = payload["id"], int(payload["compl"])
    except Exception as exc:
        return False, f"JWT не разобрался: {type(exc).__name__}"

    started = time.time()
    try:
        nonce = find_nonce(task_id, complexity)
    except RuntimeError as exc:
        _note("перебор")
        return False, str(exc)
    spent = time.time() - started
    _note("секунд", spent)

    try:
        response = session.post(VERIFY_URL,
                                json={"challenge": challenge, "nonce": nonce},
                                headers=headers, timeout=timeout)
        verified = bool(_find(response.json(), "verified"))
    except Exception as exc:
        return False, f"/verify не ответил: {type(exc).__name__}"

    if verified:
        _note("решено")
        return True, (f"проверка пройдена: сложность {complexity}, "
                      f"nonce {nonce}, {spent:.1f} с")
    _note("не подтвердилось")
    return False, f"verify отказал: {response.text[:150]}"


def report() -> str:
    with stats_lock:
        if not stats["задач"]:
            return ""
        return ("proof-of-work: задач {задач}, решено {решено}, "
                "не подтвердилось {не подтвердилось}, "
                "всего счёта {секунд:.0f} с".format(**stats))
