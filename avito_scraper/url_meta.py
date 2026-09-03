#!/usr/bin/env python3
"""
Город и категория из самой ссылки.

Разметка карточки у Avito плавает: у мобильной версии нет og:-тегов,
хлебных крошек с itemprop и половины data-marker'ов, из-за чего city,
category и address оставались пустыми. Но сам URL устроен одинаково
всегда:

    https://www.avito.ru/sankt-peterburg/avtomobili/hyundai_..._8329727762
                         ^город          ^категория                ^id

Ссылки мы и так берём из sitemap, то есть эти два поля известны ещё до
запроса и не зависят от того, какой вариант страницы отдали. Разметка
остаётся приоритетным источником — отсюда берём, только если там пусто.
"""

from __future__ import annotations

import re
import urllib.parse

# Города, где обратная транслитерация даёт заведомо не то: составные
# названия, "на-Дону", "Нижний", буква ё и прочее.
CITY_NAMES = {
    "moskva": "Москва",
    "sankt-peterburg": "Санкт-Петербург",
    "nizhniy_novgorod": "Нижний Новгород",
    "rostov-na-donu": "Ростов-на-Дону",
    "naberezhnye_chelny": "Набережные Челны",
    "nizhniy_tagil": "Нижний Тагил",
    "velikiy_novgorod": "Великий Новгород",
    "staryy_oskol": "Старый Оскол",
    "yuzhno-sahalinsk": "Южно-Сахалинск",
    "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "kamensk-uralskiy": "Каменск-Уральский",
    "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "yoshkar-ola": "Йошкар-Ола",
    "orel": "Орёл",
    "korolev": "Королёв",
    "orenburg": "Оренбург",
    "ekaterinburg": "Екатеринбург",
    "chelyabinsk": "Челябинск",
    "novosibirsk": "Новосибирск",
    "krasnoyarsk": "Красноярск",
    "vladivostok": "Владивосток",
    "habarovsk": "Хабаровск",
    "ulan-ude": "Улан-Удэ",
    "yuzhnouralsk": "Южноуральск",
    "ust-ilimsk": "Усть-Илимск",
    "sergiev_posad": "Сергиев Посад",
    "yasnyy": "Ясный",
    "mineralnye_vody": "Минеральные Воды",
    "gornoaltaysk": "Горно-Алтайск",
    "ioshkar_ola": "Йошкар-Ола",
    "kirovo-chepetsk": "Кирово-Чепецк",
    "novyy_urengoy": "Новый Уренгой",
}

# Разделы верхнего уровня. Их несколько десятков, и они меняются редко,
# поэтому список полный лучше догадок: русское название нужно точное.
CATEGORY_NAMES = {
    "avtomobili": "Автомобили",
    "mototsikly_i_mototehnika": "Мотоциклы и мототехника",
    "gruzoviki_i_spetstehnika": "Грузовики и спецтехника",
    "vodnyy_transport": "Водный транспорт",
    "zapchasti_i_aksessuary": "Запчасти и аксессуары",
    "kvartiry": "Квартиры",
    "komnaty": "Комнаты",
    "doma_dachi_kottedzhi": "Дома, дачи, коттеджи",
    "zemelnye_uchastki": "Земельные участки",
    "garazhi_i_mashinomesta": "Гаражи и машиноместа",
    "kommercheskaya_nedvizhimost": "Коммерческая недвижимость",
    "nedvizhimost_za_rubezhom": "Недвижимость за рубежом",
    "vakansii": "Вакансии",
    "rezume": "Резюме",
    "predlozheniya_uslug": "Предложения услуг",
    "uslugi": "Услуги",
    "odezhda_obuv_aksessuary": "Одежда, обувь, аксессуары",
    "detskaya_odezhda_i_obuv": "Детская одежда и обувь",
    "tovary_dlya_detey_i_igrushki": "Товары для детей и игрушки",
    "krasota_i_zdorove": "Красота и здоровье",
    "chasy_i_ukrasheniya": "Часы и украшения",
    "bytovaya_tehnika": "Бытовая техника",
    "mebel_i_interer": "Мебель и интерьер",
    "posuda_i_tovary_dlya_kuhni": "Посуда и товары для кухни",
    "produkty_pitaniya": "Продукты питания",
    "remont_i_stroitelstvo": "Ремонт и строительство",
    "rasteniya": "Растения",
    "telefony": "Телефоны",
    "audio_i_video": "Аудио и видео",
    "tovary_dlya_kompyutera": "Товары для компьютера",
    "planshety_i_elektronnye_knigi": "Планшеты и электронные книги",
    "igry_pristavki_i_programmy": "Игры, приставки и программы",
    "noutbuki": "Ноутбуки",
    "nastolnye_kompyutery": "Настольные компьютеры",
    "fototehnika": "Фототехника",
    "orgtehnika_i_rashodniki": "Оргтехника и расходники",
    "bilety_i_puteshestviya": "Билеты и путешествия",
    "velosipedy": "Велосипеды",
    "knigi_i_zhurnaly": "Книги и журналы",
    "kollektsionirovanie": "Коллекционирование",
    "muzykalnye_instrumenty": "Музыкальные инструменты",
    "ohota_i_rybalka": "Охота и рыбалка",
    "sport_i_otdyh": "Спорт и отдых",
    "sobaki": "Собаки",
    "koshki": "Кошки",
    "ptitsy": "Птицы",
    "akvarium": "Аквариум",
    "drugie_zhivotnye": "Другие животные",
    "tovary_dlya_zhivotnyh": "Товары для животных",
    "gotovyy_biznes": "Готовый бизнес",
    "oborudovanie_dlya_biznesa": "Оборудование для бизнеса",
}

# Разделы-обёртки: в URL встречаются, но названием категории быть не
# должны — под ними всегда есть что-то конкретнее.
GENERIC_SECTIONS = {"transport", "nedvizhimost", "rabota", "lichnye_veschi",
                    "dlya_doma_i_dachi", "bytovaya_elektronika", "hobbi_i_otdyh",
                    "zhivotnye", "dlya_biznesa"}

# Обратная транслитерация: сначала длинные сочетания, иначе "sch"
# распадётся на "s"+"ch". Порядок здесь значим.
TRANSLIT = [
    ("sch", "щ"), ("shch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"),
    ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("iy", "ий"), ("y", "ы"), ("a", "а"), ("b", "б"), ("v", "в"),
    ("g", "г"), ("d", "д"), ("e", "е"), ("z", "з"), ("i", "и"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("f", "ф"), ("h", "х"), ("c", "ц"), ("j", "й"), ("w", "в"),
    ("x", "кс"), ("q", "к"),
]


def detransliterate(slug: str) -> str:
    """Латиница Avito -> кириллица. Приблизительно, но читаемо.

    Нужна только как запасной вариант для городов вне словаря: их тысячи,
    и пустое поле хуже, чем «Мичуринск» с возможной опечаткой."""
    result = []
    for word in re.split(r"[-_]", slug):
        text = word.lower()
        out = ""
        position = 0
        while position < len(text):
            for latin, cyrillic in TRANSLIT:
                if text.startswith(latin, position):
                    out += cyrillic
                    position += len(latin)
                    break
            else:
                out += text[position]
                position += 1
        result.append(out[:1].upper() + out[1:] if out else "")
    return " ".join(part for part in result if part)


def path_parts(url: str) -> list:
    """Сегменты пути без последнего — последний это slug самого объявления."""
    path = urllib.parse.urlsplit(url).path.strip("/")
    if not path:
        return []
    parts = [p for p in path.split("/") if p]
    if parts and re.search(r"_\d{6,}$", parts[-1]):
        parts = parts[:-1]
    return parts


def city_from_url(url: str) -> str:
    parts = path_parts(url)
    # У настоящей карточки путь всегда /город/категория/slug_id. Если
    # сегментов меньше, это не карточка (или тестовая заглушка), и
    # выдумывать город из чего попало нельзя: так рождается "Кс 1".
    if len(parts) < 2:
        return ""
    slug = parts[0]
    if re.search(r"\d", slug):
        return ""
    if slug in ("rossiya", "all", "user", "web", "items"):
        return ""
    return CITY_NAMES.get(slug) or detransliterate(slug)


def category_from_url(url: str) -> str:
    all_parts = path_parts(url)
    if len(all_parts) < 2:
        return ""
    parts = all_parts[1:]
    if not parts:
        return ""
    # берём самый конкретный известный раздел, обёртки пропускаем
    for slug in reversed(parts):
        if slug in CATEGORY_NAMES:
            return CATEGORY_NAMES[slug]
    for slug in reversed(parts):
        if slug not in GENERIC_SECTIONS:
            return detransliterate(slug)
    return CATEGORY_NAMES.get(parts[0], detransliterate(parts[0]))
