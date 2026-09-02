"""
Опциональная интеграция с сервисом решения капч (rucaptcha.com — API
совместим с 2captcha). Включается переменной окружения RUCAPTCHA_API_KEY;
без неё все функции ниже — no-op, и scraper.py ведёт себя как раньше
(останавливается при обнаружении капчи/антибота).

НИКОГДА не храните сам ключ в коде/репозитории — только через переменную
окружения:

    export RUCAPTCHA_API_KEY="ваш_ключ"
    python scraper.py scrape --limit 10 --no-headless

ВАЖНО: конкретный тип защиты, который Avito показывает подозрительному
трафику, здесь не проверялся вживую (в среде разработки не было доступа к
avito.ru). Поддержаны самые частые типы — reCAPTCHA v2 и hCaptcha. Если
Avito показывает что-то другое (свой JS-челлендж, GeeTest и т.п.),
`solve_if_present()` вернёт False и в логе будет сообщение — тогда нужно
посмотреть на реальную HTML-страницу блокировки и доработать эту функцию.
"""

from __future__ import annotations

import os
import time

from playwright.sync_api import Page

API_KEY = os.environ.get("RUCAPTCHA_API_KEY", "").strip()
SERVER = os.environ.get("RUCAPTCHA_SERVER", "rucaptcha.com").strip()
MAX_ATTEMPTS = int(os.environ.get("RUCAPTCHA_MAX_ATTEMPTS", "3"))

_solver = None


def enabled() -> bool:
    return bool(API_KEY)


def _get_solver():
    global _solver
    if _solver is None:
        from twocaptcha import TwoCaptcha  # пакет "2captcha-python"
        _solver = TwoCaptcha(API_KEY, server=SERVER)
    return _solver


def _inject_recaptcha_token(page: Page, token: str) -> None:
    page.evaluate(
        """(token) => {
            const ta = document.getElementById('g-recaptcha-response')
                || document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta) {
                ta.style.display = 'block';
                ta.value = token;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
            }
            // популярные имена колбэков, которые сайты вызывают после решения
            for (const name of ['onCaptchaSuccess', 'captchaCallback', 'onRecaptchaSuccess']) {
                if (typeof window[name] === 'function') {
                    try { window[name](token); } catch (e) {}
                }
            }
            const form = ta ? ta.closest('form') : null;
            if (form) {
                try { form.requestSubmit ? form.requestSubmit() : form.submit(); } catch (e) {}
            }
        }""",
        token,
    )


def _inject_hcaptcha_token(page: Page, token: str) -> None:
    page.evaluate(
        """(token) => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]')
                || document.getElementById('h-captcha-response');
            if (ta) {
                ta.value = token;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        token,
    )


def solve_if_present(page: Page) -> bool:
    """Пытается распознать капчу на текущей странице и решить её через
    rucaptcha. Возвращает True, если что-то решили (страницу стоит
    перезагрузить и проверить заново); False — капчу не нашли или не
    смогли решить (решение отключено, неизвестный тип виджета, ошибка API)."""
    if not enabled():
        return False

    sitekey_el = page.query_selector("[data-sitekey]")
    if not sitekey_el:
        print("  -> капча/блокировка есть, но виджет с data-sitekey не найден — "
              "нужно посмотреть на реальную разметку страницы блокировки, "
              "чтобы добавить поддержку конкретного типа защиты.")
        return False

    sitekey = sitekey_el.get_attribute("data-sitekey")
    if not sitekey:
        return False

    is_hcaptcha = (
        "hcaptcha" in (sitekey_el.get_attribute("class") or "").lower()
        or page.query_selector("script[src*='hcaptcha']") is not None
    )

    # hCaptcha Enterprise (частый выбор у крупных сайтов вроде Avito) требует
    # ещё и rqdata, без него сервис решения физически не может выдать
    # валидный токен — отсюда возможен ERROR_CAPTCHA_UNSOLVABLE даже когда
    # sitekey найден верно.
    extra: dict = {}
    rqdata_el = page.query_selector("[data-rqdata]")
    if rqdata_el:
        rqdata = rqdata_el.get_attribute("data-rqdata")
        if rqdata:
            extra["rqdata"] = rqdata

    solver = _get_solver()
    kind = "hCaptcha" if is_hcaptcha else "reCAPTCHA"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"  -> нашёл {kind} (sitekey={sitekey[:12]}...), попытка {attempt}/{MAX_ATTEMPTS} "
              f"через rucaptcha, ждём решения...")
        try:
            if is_hcaptcha:
                result = solver.hcaptcha(sitekey=sitekey, url=page.url, **extra)
                _inject_hcaptcha_token(page, result["code"])
            else:
                result = solver.recaptcha(sitekey=sitekey, url=page.url, **extra)
                _inject_recaptcha_token(page, result["code"])
        except Exception as exc:
            print(f"  -> попытка {attempt} не удалась: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(5)
            continue

        print("  -> капча решена, перезагружаю страницу")
        return True

    print(f"  -> не удалось решить капчу через rucaptcha за {MAX_ATTEMPTS} попыт(ки/ок). "
          "Если это повторяется стабильно — вероятно, дело не в самой капче, а в том, что "
          "IP этого сервера уже помечен антиботом Avito как подозрительный "
          "(похоже на это, раз капча вылезла на самом первом запросе).")
    return False
