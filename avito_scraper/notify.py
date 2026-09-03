#!/usr/bin/env python3
"""
Отправка хода сбора в Telegram: порциями по ходу дела и файлом в конце.

Смысл в том, чтобы не сидеть у терминала часами. Каждые N строк уходит
файл с этой порцией — видно, что собирается именно то, что нужно, и
можно вмешаться на пятидесятой строке, а не на тридцатитысячной. В конце
приходит весь CSV целиком.

Токен берётся из TELEGRAM_TOKEN. Идентификатор чата — из TELEGRAM_CHAT,
а если его нет, определяется сам: достаточно написать боту /start, и
скрипт заберёт chat_id из getUpdates. Это избавляет от возни с поиском
своего id.

Ни токен, ни chat_id в файлы проекта не пишутся.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"

_chat_cache = ""


def token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "").strip()


def enabled() -> bool:
    return bool(token())


def _call(method: str, fields: dict, timeout: int = 30) -> dict:
    url = API.format(token=token(), method=method)
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def chat_id() -> str:
    """Из переменной окружения, иначе — из последнего сообщения боту."""
    global _chat_cache
    explicit = os.environ.get("TELEGRAM_CHAT", "").strip()
    if explicit:
        return explicit
    if _chat_cache:
        return _chat_cache
    try:
        updates = _call("getUpdates", {"limit": 10})
    except Exception:
        return ""
    for update in reversed(updates.get("result", [])):
        message = update.get("message") or update.get("channel_post") or {}
        found = str((message.get("chat") or {}).get("id", ""))
        if found:
            _chat_cache = found
            return found
    return ""


def send_message(text: str) -> str:
    if not enabled():
        return "TELEGRAM_TOKEN не задан"
    chat = chat_id()
    if not chat:
        return "напишите боту /start — иначе он не знает, кому отвечать"
    try:
        result = _call("sendMessage", {"chat_id": chat, "text": text[:4000]})
        return "отправлено" if result.get("ok") else str(result)[:120]
    except Exception as exc:
        return f"не отправилось: {type(exc).__name__}"


def build_multipart(chat: str, caption: str, path: Path, boundary: str = ""):
    """Тело запроса с файлом. Отдельно от отправки — чтобы проверялось.

    Тянуть ради одного запроса зависимость с поддержкой multipart смысла
    нет: тело здесь простое и целиком видно. Но собранное вручную тело
    легко сломать невидимой мелочью вроде пропущенного \\r\\n, поэтому
    сборка вынесена в функцию и покрыта тестом."""
    boundary = boundary or uuid.uuid4().hex
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = []
    for name, value in (("chat_id", chat), ("caption", caption[:1000])):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
            f"\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_file(path, caption: str = "") -> str:
    if not enabled():
        return "TELEGRAM_TOKEN не задан"
    chat = chat_id()
    if not chat:
        return "напишите боту /start"

    path = Path(path)
    if not path.exists():
        return f"нет файла {path}"

    body, content_type = build_multipart(chat, caption, path)
    request = urllib.request.Request(
        API.format(token=token(), method="sendDocument"), data=body,
        headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
        return "отправлено" if result.get("ok") else str(result)[:120]
    except Exception as exc:
        return f"не отправилось: {type(exc).__name__}"
