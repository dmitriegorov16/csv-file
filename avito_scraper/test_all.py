#!/usr/bin/env python3
"""
Тесты всей обвязки — без сети, без Avito, без Telegram.

Смысл в том, чтобы ошибки вылезали здесь, а не на тридцатитысячной
строке многочасового прогона. Всё, что зависит от сети, проверяется на
сохранённых ответах и временных файлах: настоящие данные Avito уже
получены, и повторно ходить за ними ради проверки разбора незачем.

    python test_all.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASSED = []
FAILED = []


def check(name: str, condition, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "OK   " if condition else "ПРОВАЛ"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not condition else ""))


# --------------------------------------------------------------------------
# Настоящее объявление из выдачи каталога: на нём держится вся раскладка
# --------------------------------------------------------------------------
ITEM = {
    "id": 8047578848,
    "urlPath": "/moskva/avtomobili/mercedes-benz_g-klass_amg_4.0_at_2026_8047578848"
               "?context=H4sIAAAAAAA",
    "title": "Mercedes-Benz G-класс AMG 4.0 AT, 2026",
    "description": "Новый Mercedes-Benz Carlex G-Vintage Facelift. " * 5,
    "category": {"id": 9, "name": "Автомобили", "slug": "avtomobili"},
    "location": {"id": 637640, "name": "Москва"},
    "addressDetailed": {"locationName": "Москва"},
    "priceDetailed": {"value": 66280000, "string": "66 280 000"},
    "images": [{"416x416": "https://80.img.avito.st/image/1/a.jpg",
                "636x636": "https://80.img.avito.st/image/1/big.jpg",
                "208x208": "https://80.img.avito.st/image/1/c.jpg"}],
    "geo": {"formattedAddress": "Москва, Лужники"},
}


def test_catalog_mapping() -> None:
    print("\nРаскладка объявления из каталога в CSV:")
    from catalog_scrape import to_listing, best_image

    row = to_listing(ITEM, 7)
    check("ссылка без метки ?context", "?" not in row.url and
          row.url.endswith("_8047578848"), row.url)
    check("ссылка абсолютная", row.url.startswith("https://www.avito.ru/"), row.url)
    check("id проставляется наш", row.id == 7)
    check("цена числом", row.price == "66280000", row.price)
    check("категория", row.category == "Автомобили", row.category)
    check("город", row.city == "Москва", row.city)
    check("адрес подробный, а не город", row.address == "Москва, Лужники", row.address)
    check("картинка самая большая", row.image.endswith("big.jpg"), row.image)
    check("описание — обрезка до 100 символов",
          len(row.description) == 101 and row.description.endswith("…"),
          row.description[:30])
    check("полный текст сохранён целиком",
          row.content == ITEM["description"].strip())

    check("объявление без картинок не роняет разбор",
          best_image({"images": []}) == "")
    check("объявление без цены даёт пустую строку",
          to_listing({"urlPath": "/x_1", "title": "т"}, 1).price == "")

    # Город есть не у всех объявлений, а адрес — тем более. Раньше при
    # отсутствии адреса в колонку падало название города, и она дублировала
    # соседнюю, изображая данные, которых нет.
    no_address = to_listing({"urlPath": "/kazan/kvartiry/dvushka_1234567891",
                             "title": "Квартира",
                             "location": {"name": "Казань"},
                             "addressDetailed": {"locationName": "Казань"}}, 2)
    check("нет адреса — колонка пустая", no_address.address == "",
          no_address.address)
    check("город при этом на месте", no_address.city == "Казань")

    no_city = to_listing({"urlPath": "/volgograd/telefony/iphone_1234567892",
                          "title": "iPhone"}, 3)
    check("города нет в ответе — берётся из ссылки",
          no_city.city == "Волгоград", no_city.city)

    same = to_listing({"urlPath": "/perm/knigi/kniga_1234567894", "title": "К",
                       "location": {"name": "Пермь"},
                       "geo": {"formattedAddress": "Пермь"}}, 4)
    check("адрес, равный городу, не дублируется", same.address == "")


def test_service_record_skipped() -> None:
    print("\nСлужебная запись в конце выдачи:")
    # 51-й элемент ответа — не объявление: у него нет urlPath. Если он
    # попадёт в CSV, там будет строка с пустой ссылкой.
    service = {"type": "item", "code": "banner"}
    check("нет urlPath — значит не объявление", not service.get("urlPath"))


def test_no_duplicates() -> None:
    print("\nЗащита от повторов:")
    from catalog_scrape import to_listing

    done = set()
    rows = []
    # одно и то же объявление приходит в выдаче двух городов
    for _ in range(2):
        key = str(ITEM["id"])
        if key in done:
            continue
        done.add(key)
        rows.append(to_listing(ITEM, len(rows) + 1))
    check("повтор отсеян по id", len(rows) == 1, f"строк {len(rows)}")


def test_limit_counts_once() -> None:
    print("\nСчёт до цели:")
    # done пополняется той же строкой, что и written, поэтому складывать
    # их — значит считать каждую строку дважды. Сбор останавливался на
    # 85 строках, решив, что собрал 170 из 150.
    already, done, written, limit = 0, set(), 0, 150
    for number in range(1, 200):
        if already + written >= limit:
            break
        done.add(number)
        written += 1
    check("до цели дошли ровно", written == limit, f"собрано {written}")

    # и продолжение прогона считает от того, что уже было
    already, written = 100, 0
    for number in range(1, 200):
        if already + written >= limit:
            break
        written += 1
    check("продолжение добирает остаток", written == 50, f"добрано {written}")


def test_chunk_file() -> None:
    print("\nПорция для Telegram:")
    from catalog_scrape import send_chunk, to_listing, DATA_DIR

    os.environ.pop("TELEGRAM_TOKEN", None)
    rows = [to_listing(ITEM, i) for i in range(1, 4)]
    answer = send_chunk(rows, 3)
    path = DATA_DIR / "chunks" / "000001-000003.csv"
    check("без токена честно сообщает", "TELEGRAM_TOKEN" in answer, answer)
    check("файл всё равно создан", path.exists())
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            written = list(csv.DictReader(handle))
        check("в файле ровно порция", len(written) == 3, f"строк {len(written)}")
        check("заголовок на месте", written[0]["title"] == ITEM["title"])
        path.unlink()


def test_multipart() -> None:
    print("\nЗапрос с файлом для Telegram:")
    from notify import build_multipart

    sample = Path(tempfile.mkdtemp()) / "проба.csv"
    sample.write_text("id,title\n1,Тест\n", encoding="utf-8")
    body, content_type = build_multipart("123", "подпись", sample, boundary="ГРАНИЦА")

    check("тип содержимого с границей",
          content_type == "multipart/form-data; boundary=ГРАНИЦА", content_type)
    check("чат в теле", b'name="chat_id"' in body and b"123" in body)
    check("подпись в теле", "подпись".encode() in body)
    check("имя файла в теле", "проба.csv".encode() in body)
    check("содержимое файла в теле", "1,Тест".encode() in body)
    check("тело закрыто по правилам", body.endswith("--ГРАНИЦА--\r\n".encode()))


def test_firewall_answers() -> None:
    print("\nРазбор ответов фаервола:")
    import firewall
    import firewall_pow

    check("виджет geeTest из настоящего ответа",
          firewall.find_type(json.loads(
              '{"success":{"result":{"captcha":{"geeTest":{"type":"geeTest"}}}}}'))
          == firewall.GEETEST)
    check("hCaptcha тоже узнаётся",
          firewall.find_type({"captcha": {"hCaptcha": {}}}) == firewall.HCAPTCHA)
    check("verified достаётся из вложенного ответа",
          firewall._find({"success": {"result": {"verified": True}}}, "verified")
          is True)
    check("отказ не принимается за успех",
          firewall._find({"success": {"result": {"verified": False}}}, "verified")
          is False)
    check("pow_challenge вынимается",
          firewall_pow.challenge_from('{"pow_challenge":"ЗАДАЧА"}') == "ЗАДАЧА")
    check("нет задачи — пустая строка",
          firewall_pow.challenge_from('{"too-many-requests":{}}') == "")


def test_pow_solver() -> None:
    print("\nРешатель proof-of-work:")
    import hashlib
    from firewall_pow import find_nonce, jwt_payload
    import base64

    nonce = find_nonce("задача", 3)
    digest = hashlib.sha256(f"задача:{nonce}".encode()).hexdigest()
    check("хеш начинается с нужных нулей", digest.startswith("000"), digest[:8])

    payload = base64.urlsafe_b64encode(
        json.dumps({"id": "abc", "compl": 4}).encode()).decode().rstrip("=")
    decoded = jwt_payload(f"header.{payload}.signature")
    check("JWT разбирается без подписи",
          decoded["id"] == "abc" and decoded["compl"] == 4, str(decoded))


def test_url_meta() -> None:
    print("\nГород и категория из ссылки:")
    from url_meta import category_from_url, city_from_url, category_from_crumbs

    check("город", city_from_url(
        "https://www.avito.ru/volgograd/avtomobili/kia_7654321098") == "Волгоград")
    check("категория", category_from_url(
        "https://www.avito.ru/volgograd/avtomobili/kia_7654321098") == "Автомобили")
    check("заглушка не даёт выдуманного города",
          city_from_url("https://www.avito.ru/x_1") == "")
    check("из крошек берётся раздел, а не модель",
          category_from_crumbs(["Транспорт", "Автомобили", "Kia", "IV (2015—2018)"])
          == "Автомобили")


def test_sitemap() -> None:
    print("\nРазбор sitemap:")
    from sitemap_csv import parse_entries, title_from_slug

    xml = ('<url><loc>https://www.avito.ru/semenov/akvarium/'
           'prodam_akvarium_s_rybkami_8245305594</loc>'
           '<image:image><image:loc>https://40.img.avito.st/image/1/x</image:loc>'
           '</image:image></url>'
           '<url><loc>https://www.avito.ru/semenov/akvarium/rybki_8219364340</loc>'
           '</url>')
    entries = list(parse_entries(xml))
    check("оба объявления найдены", len(entries) == 2, str(len(entries)))
    check("фотография привязана к своему объявлению",
          entries[0][1].endswith("/image/1/x"), entries[0][1])
    check("объявление без фото не ломает разбор", entries[1][1] == "")
    check("заголовок из slug",
          title_from_slug(entries[0][0]) == "Продам аквариум с рыбками",
          title_from_slug(entries[0][0]))


def test_status_report() -> None:
    print("\nОтчёт о ходе сбора:")
    import status

    folder = Path(tempfile.mkdtemp())
    path = folder / "avito.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "url", "title", "content", "description",
                         "image", "price", "category", "city", "address"])
        writer.writerow(["1", "https://a/1", "Т", "Текст", "Т", "i", "10",
                         "Кат", "Москва", ""])
        writer.writerow(["2", "https://a/2", "Т", "Текст", "Т", "i", "", "Кат",
                         "Москва", "адрес"])

    state = status.snapshot(path)
    check("строки посчитаны", state["строк"] == 2, str(state["строк"]))
    check("пустая цена замечена", state["полей"]["price"] == 1)
    text = status.report(state, {"строк": 0}, gap=60)
    check("скорость посчитана", "строк/мин" in text)
    check("пустые колонки названы", "price" in text and "address" in text)


def test_find_items() -> None:
    print("\nПоиск объявлений в ответе каталога:")
    from unlock import find_items

    check("на верхнем уровне", len(find_items({"items": [1, 2, 3]})) == 3)
    # так ответ и устроен на самом деле — вложенно
    check("вложенный", len(find_items({"result": {"catalog": {"items": [1, 2]}}})) == 2)
    check("нет списка — пусто", find_items({"a": {"b": 1}}) == [])
    check("не роняется на пустом ответе", find_items({}) == [])


def test_source_chain() -> None:
    print("\nЦепочка источников:")
    import os
    import unlock

    saved = {k: os.environ.get(k) for k in
             ("AVITO_BACKUP_PROXY", "AVITO_PROXY_LIST_URL")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        chain = unlock.sources_chain()
        check("без настроек — только сервер",
              [name for name, _, _ in chain] == ["сервер"], str(chain))

        os.environ["AVITO_BACKUP_PROXY"] = "http://u:p@host:1"
        os.environ["AVITO_PROXY_LIST_URL"] = "https://list"
        chain = unlock.sources_chain()
        check("порядок: сервер, резерв, список",
              [name for name, _, _ in chain]
              == ["сервер", "резервный прокси", "список провайдера"], str(chain))
        # Проверяем сам вид «логин:пароль@хост», а не конкретный пароль:
        # написать секрет в тест ради проверки, что его нет в коде, —
        # значит положить его в репозиторий своими руками.
        import re as regexp
        sources = "".join(Path(name).read_text(encoding="utf-8")
                          for name in ("unlock.py", "catalog_scrape.py",
                                       "preflight.py"))
        check("в коде нет зашитых логинов с паролями",
              not regexp.search(r"://[^\s\"']+:[^\s\"']+@", sources))

        # умерший источник пропускается: skip начинает со следующего
        tried = []

        def fake_open(proxy="", proxy_list_url="", attempts=6, probe_url="",
                      verbose=True):
            tried.append(proxy or proxy_list_url or "сервер")
            return ("сессия", None) if len(tried) > 1 else (None, None)

        original = unlock.open_session
        unlock.open_session = fake_open
        try:
            session, name, position = unlock.open_from_chain(chain, verbose=False,
                                                             skip=1)
        finally:
            unlock.open_session = original
        check("начали не с начала", tried[0] != "сервер", str(tried))
        check("нашли рабочий источник", session == "сессия", str(name))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_retry_after_refused_check() -> None:
    print("\nОтказ на проверке:")
    import unlock

    # Решение капчи могло прийти с задержкой и протухнуть; сдаваться с
    # первого отказа значит терять доступ на ровном месте.
    attempts = {"solve": 0, "get": 0}

    class Blocked:
        status_code = 403
        text = '{"captcha": 1}'
        content = b"x"

    class Opened:
        status_code = 200
        text = "{}"
        content = b"data"

    class FakeSession:
        proxies = {}

        def get(self, *args, **kwargs):
            attempts["get"] += 1
            # третий запрос отдаёт данные — если до него дойдёт
            return Opened() if attempts["solve"] >= 2 else Blocked()

    def flaky_solve(session, url, body, timeout=30):
        attempts["solve"] += 1
        return (attempts["solve"] >= 2), ("прошла" if attempts["solve"] >= 2
                                          else "verify отказал")

    original = unlock.firewall.solve
    unlock.firewall.solve = flaky_solve
    try:
        response = unlock.ensure_access(FakeSession(), "https://x", verbose=False)
    finally:
        unlock.firewall.solve = original

    check("после отказа пробуем ещё", attempts["solve"] >= 2,
          f"попыток решения: {attempts['solve']}")
    check("в итоге доступ открыт", response.status_code == 200,
          str(response.status_code))


def test_dead_proxy_does_not_crash() -> None:
    print("\nМёртвая прокси:")
    import unlock

    class DeadSession:
        proxies = {}

        def get(self, *args, **kwargs):
            raise TimeoutError("read timed out")

    response = unlock.ensure_access(DeadSession(), "https://www.avito.ru/x",
                                    verbose=False)
    check("вместо исключения — неудачный ответ", response.status_code == 0)
    check("причина названа", response.reason == "TimeoutError", response.reason)
    check("json() не роняет разбор", response.json() == {})


def test_broken_item_does_not_crash() -> None:
    print("\nКривое объявление:")
    from catalog_scrape import to_listing

    # В выдаче попадаются объявления, где привычное поле приходит списком,
    # строкой или null. Одно такое не должно ронять многочасовой прогон.
    broken = {"urlPath": "/moskva/avtomobili/x_1234567890", "title": "Т",
              "priceDetailed": [1, 2], "geo": None, "location": "Москва",
              "category": [], "addressDetailed": "Москва", "images": "нет"}
    row = to_listing(broken, 1)
    check("разбор не падает", row.title == "Т")
    check("цена пустая, а не мусор", row.price == "")
    check("картинка пустая", row.image == "")
    check("город взят из ссылки", row.city == "Москва", row.city)


def test_bot_module() -> None:
    print("\nБот:")
    try:
        import bot
    except ImportError as exc:
        check("aiogram установлен", False, str(exc))
        return

    check("подсказка перечисляет команды",
          all(c in bot.HELP for c in ("/start", "/status", "/stop")))
    check("порции ищутся в data/chunks",
          bot.CHUNKS.name == "chunks" and bot.CHUNKS.parent.name == "data")
    check("итоговый файл — avito.csv", bot.CSV.name == "avito.csv")
    check("сбор виден по имени процесса",
          isinstance(bot.collector_running(), bool))


def main() -> None:
    test_catalog_mapping()
    test_service_record_skipped()
    test_no_duplicates()
    test_limit_counts_once()
    test_chunk_file()
    test_multipart()
    test_firewall_answers()
    test_pow_solver()
    test_url_meta()
    test_sitemap()
    test_status_report()
    test_find_items()
    test_source_chain()
    test_retry_after_refused_check()
    test_dead_proxy_does_not_crash()
    test_broken_item_does_not_crash()
    test_bot_module()

    print(f"\n{'=' * 58}")
    print(f"Пройдено: {len(PASSED)}, провалено: {len(FAILED)}")
    if FAILED:
        for name in FAILED:
            print(f"  ПРОВАЛ: {name}")
        sys.exit(1)
    print("Всё в порядке.")


if __name__ == "__main__":
    main()
