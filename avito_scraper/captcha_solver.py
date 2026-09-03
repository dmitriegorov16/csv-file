"""
Опциональная интеграция с сервисом решения капч (rucaptcha.com — API
совместим с 2captcha). Включается переменной окружения RUCAPTCHA_API_KEY;
без неё все функции ниже — no-op, и scraper.py ведёт себя как раньше
(останавливается при обнаружении капчи/антибота).

НИКОГДА не храните сам ключ в коде/репозитории — только через переменную
окружения:

    export RUCAPTCHA_API_KEY="ваш_ключ"
    python scraper.py scrape --limit 10

Реализация подогнана под реальную firewall-страницу Avito (проверено на
дампе HTML боевой блокировки): JS на странице асинхронно решает, какой
из трёх виджетов показать — hCaptcha, GeeTest v4 или собственный
image-captcha ("internalCaptcha") — и отправляет решение не обычным
сабмитом формы, а через свою функцию verifyCaptcha(). Ниже это
воспроизведено: дожидаемся, какой виджет реально показан, решаем именно
его и эмулируем тот же JS-флоу, что использует сама страница.

Если Avito поменяет разметку firewall-страницы — proще всего снять новый
дамп (см. README, раздел "Прокси"/troubleshooting) и поправить
`_ACTIVE_WIDGET_JS` / `_submit_*` под актуальный код.
"""

from __future__ import annotations

import json
import os
import re
import time

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

API_KEY = os.environ.get("RUCAPTCHA_API_KEY", "").strip()
SERVER = os.environ.get("RUCAPTCHA_SERVER", "rucaptcha.com").strip()
MAX_ATTEMPTS = int(os.environ.get("RUCAPTCHA_MAX_ATTEMPTS", "3"))

_solver = None

CAPTCHA_ID_RE = re.compile(r"captchaId\s*=\s*'([a-f0-9]+)'")

# JS-выражение: если один из известных виджетов Avito уже показан —
# возвращает его тип, иначе null (ещё не решил getCaptcha() / другая
# страница).
_ACTIVE_WIDGET_JS = """
() => {
    const shown = (el) => el && getComputedStyle(el).display !== 'none'
                       && el.offsetParent !== null;

    // 1. Разметка десктопной страницы фаервола: три контейнера с
    //    предсказуемыми id, из которых JS показывает ровно один.
    const h = document.querySelector('#h-captcha');
    const g = document.querySelector('#geetest_captcha');
    const i = document.querySelector('#inner-captcha');
    if (shown(h)) return 'hcaptcha';
    if (shown(g)) return 'geetest';
    if (shown(i)) return 'internal';

    // 2. Мобильная версия сайта отдаёт другую страницу — там этих id нет
    //    (мы это видели: firewall_form=False). Поэтому ищем по признакам
    //    самих виджетов, а не по вёрстке Avito вокруг них.
    if (document.querySelector('iframe[src*="geetest"], script[src*="gt4"], '
                             + 'script[src*="geetest"], [class*="geetest"]'))
        return 'geetest';
    if (document.querySelector('iframe[src*="hcaptcha"], script[src*="hcaptcha"], '
                             + '.h-captcha, [data-hcaptcha-sitekey]'))
        return 'hcaptcha';
    if (document.querySelector('[data-sitekey]'))
        return 'hcaptcha';
    if (document.querySelector('img[src*="captcha"], input[name*="captcha"]'))
        return 'internal';
    return null;
}
"""


def enabled() -> bool:
    return bool(API_KEY)


def _get_solver():
    global _solver
    if _solver is None:
        from twocaptcha import TwoCaptcha  # пакет "2captcha-python"
        _solver = TwoCaptcha(API_KEY, server=SERVER)
    return _solver


def _wait_for_active_widget(page: Page, timeout_ms: int = 15000):
    """getCaptcha() на странице Avito асинхронный — ждём, пока JS решит,
    какой виджет показать, и возвращаем его тип ('hcaptcha'/'geetest'/
    'internal') или None, если не дождались/это не тот firewall."""
    try:
        page.wait_for_function(
            "() => (" + _ACTIVE_WIDGET_JS.strip() + ")() !== null",
            timeout=timeout_ms,
        )
    except PWTimeoutError:
        return None
    return page.evaluate(_ACTIVE_WIDGET_JS)


def _submit_and_wait(page: Page, inject_js: str, token) -> bool:
    """Выполняет inject_js(token) — он должен положить решение в нужное
    поле и вызвать сабмит формы firewall'а — и ждёт, что после этого
    страница перезагрузится (так делает сама Avito при успешной проверке:
    ставит cookie captcha_solved и через ~300мс делает location.reload).
    Возвращает True, если дождались перезагрузки."""
    try:
        with page.expect_navigation(timeout=20000):
            page.evaluate(inject_js, token)
        return True
    except PWTimeoutError:
        return False


def _solve_hcaptcha(page: Page, solver) -> bool:
    sitekey_el = (page.query_selector("#h-captcha[data-sitekey]")
                  or page.query_selector("[data-sitekey]")
                  or page.query_selector("[data-hcaptcha-sitekey]"))
    sitekey = None
    if sitekey_el:
        sitekey = (sitekey_el.get_attribute("data-sitekey")
                   or sitekey_el.get_attribute("data-hcaptcha-sitekey"))
    if not sitekey:
        print("  -> ожидался hCaptcha, но data-sitekey не найден")
        return False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"  -> hCaptcha (sitekey={sitekey[:12]}...), попытка {attempt}/{MAX_ATTEMPTS} через rucaptcha...")
        try:
            result = solver.hcaptcha(sitekey=sitekey, url=page.url)
        except Exception as exc:
            print(f"  -> попытка {attempt} не удалась: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(5)
            continue

        token = result["code"]
        ok = _submit_and_wait(
            page,
            """(token) => {
                if (typeof window.sethCaptchaResponse === 'function') {
                    window.sethCaptchaResponse(token);
                }
                const ta = document.querySelector('#h-captcha-response')
                    || document.querySelector('textarea[name="h-captcha-response"]');
                if (ta) { ta.value = token; }
                const form = document.querySelector('.js-firewall-form')
                    || (ta && ta.closest('form')) || document.querySelector('form');
                if (form) {
                    form.dispatchEvent(new Event('submit'));
                    try { form.requestSubmit ? form.requestSubmit() : form.submit(); }
                    catch (e) {}
                }
            }""",
            token,
        )
        if ok:
            print("  -> hCaptcha принята, страница перезагрузилась")
            return True
        print("  -> решение отправлено, но страница не подтвердила (перезагрузки не было)")

    return False


def _solve_geetest(page: Page, solver) -> bool:
    match = CAPTCHA_ID_RE.search(page.content())
    if not match:
        print("  -> ожидался GeeTest, но captchaId не найден в разметке")
        return False
    captcha_id = match.group(1)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"  -> GeeTest v4 (captcha_id={captcha_id[:12]}...), попытка {attempt}/{MAX_ATTEMPTS} через rucaptcha...")
        try:
            result = solver.geetest_v4(captcha_id=captcha_id, url=page.url)
        except Exception as exc:
            print(f"  -> попытка {attempt} не удалась: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(5)
            continue

        try:
            payload = json.loads(result["code"]) if isinstance(result["code"], str) else dict(result["code"])
        except (ValueError, TypeError) as exc:
            print(f"  -> не смог разобрать ответ rucaptcha для GeeTest: {exc} (сырой ответ: {result.get('code')!r})")
            continue
        payload["captcha_id"] = captcha_id

        ok = _submit_and_wait(
            page,
            """(payload) => {
                const ta = document.querySelector('input[name="captcha-response"]');
                if (ta) { ta.value = JSON.stringify(payload); }
                const form = document.querySelector('.js-firewall-form')
                    || (ta && ta.closest('form')) || document.querySelector('form');
                if (form) {
                    form.dispatchEvent(new Event('submit'));
                    try { form.requestSubmit ? form.requestSubmit() : form.submit(); }
                    catch (e) {}
                }
            }""",
            payload,
        )
        if ok:
            print("  -> GeeTest принят, страница перезагрузилась")
            return True
        print("  -> решение отправлено, но страница не подтвердила (перезагрузки не было)")

    return False


def solve_if_present(page: Page) -> bool:
    """Пытается распознать капчу на текущей странице firewall'а Avito и
    решить её через rucaptcha. Возвращает True, если по итогу страница
    больше не выглядит заблокированной; False — капчу не нашли/не решили
    (решение отключено, неизвестный/неподдерживаемый тип виджета, ошибка
    API, сервис не смог решить)."""
    if not enabled():
        return False

    widget = _wait_for_active_widget(page)
    if widget is None:
        print("  -> капча/блокировка есть, но виджета не видно за 15с. "
              "Скорее всего это другая версия страницы (например мобильная).")
        try:
            from pathlib import Path
            out = Path(__file__).resolve().parent / "data" / "unknown_block.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(page.content(), encoding="utf-8")
            print(f"  -> страница сохранена: {out} — по ней добавим поддержку")
        except Exception:
            pass
        return False

    solver = _get_solver()

    if widget == "hcaptcha":
        solved = _solve_hcaptcha(page, solver)
    elif widget == "geetest":
        solved = _solve_geetest(page, solver)
    else:
        print("  -> Avito показал собственный image-captcha (internalCaptcha) — "
              "автоматическое решение этого типа пока не реализовано.")
        return False

    if not solved:
        print(f"  -> не удалось решить {widget} за {MAX_ATTEMPTS} попыт(ки/ок). "
              "Если это повторяется стабильно на разных IP — возможно, проблема не "
              "в самой капче.")
        return False

    return not is_blocked_after_solve(page)


def is_blocked_after_solve(page: Page) -> bool:
    # локальный импорт, чтобы не тянуть scraper.py в captcha_solver.py на
    # уровне модуля (там наоборот импортируется этот файл)
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    # только заголовки страниц-заглушек фаервола; голое слово "captcha"
    # сюда не годится — оно встречается и на нормальных страницах
    markers = ["доступ ограничен", "проверка безопасности",
               "подтвердите, что вы не робот", "проверка браузера"]
    return any(m in title for m in markers)
