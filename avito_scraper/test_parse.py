import sys, types
sync_api = types.ModuleType('playwright.sync_api')
class _E(Exception): pass
sync_api.sync_playwright=lambda:None; sync_api.Page=object; sync_api.TimeoutError=_E
sys.modules['playwright']=types.ModuleType('playwright'); sys.modules['playwright.sync_api']=sync_api
sys.modules['captcha_solver']=types.ModuleType('captcha_solver')
import fast_scrape as fs

HTML = '''<html><head>
<meta property="og:title" content="Hyundai Solaris 1.6 AT, 2019, 395 000 км | Авито"/>
<meta name="description" content="Продам Hyundai Solaris в отличном состоянии, один владелец."/>
<meta property="og:image" content="https://80.img.avito.st/image/1/abc.jpg"/>
<script type="application/ld+json">
{"@type":"Product","name":"Hyundai Solaris 1.6 AT","category":"Автомобили",
 "offers":{"@type":"Offer","price":"1250000","priceCurrency":"RUB",
  "availableAtOrFrom":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"санкт-Петербург"}}}}
</script></head><body>
<span itemprop="name">Все объявления</span>
<span itemprop="name">Транспорт</span>
<span itemprop="name">Автомобили</span>
<h1 data-marker="item-view/title-info">Hyundai Solaris 1.6 AT, 2019</h1>
<div data-marker="item-view/item-price">1 250 000 &#8381;</div>
<div data-marker="item-view/item-description"><p>Машина в отличном состоянии.</p><p>Один владелец, все ТО.</p></div>
<div data-marker="item-view/item-address"><span>Санкт-Петербург, Невский проспект, 1</span></div>
</body></html>'''

r = fs.parse_html(HTML, 'https://www.avito.ru/spb/avtomobili/x_8329727762', 1)
print('Разбор реалистичной карточки:')
for k, v in vars(r).items():
    v = str(v)
    print(f'  {k:12} {v[:70]}')

assert r.title == 'Hyundai Solaris 1.6 AT, 2019, 395 000 км', f'title: {r.title!r}'
assert r.price == '1250000', f'price: {r.price!r}'
assert r.city == 'Санкт-Петербург', f'city: {r.city!r}'   # с заглавной, как в ТЗ
assert r.category == 'Автомобили', f'category: {r.category!r}'
assert 'отличном состоянии' in r.content, f'content: {r.content!r}'
assert r.image.endswith('abc.jpg')
assert r.address.startswith('Санкт-Петербург, Невский')
assert r.description.startswith('Продам Hyundai')
print('\n[1] полная карточка — OK')

# без JSON-LD и без meta description: цена из блока, description обрезается
HTML2 = '''<html><head><meta property="og:title" content="Диван б/у"/></head><body>
<div data-marker="item-view/item-price">15 000 &#8381;</div>
<div data-marker="item-view/item-description">''' + ('очень длинный текст, ' * 20) + '''</div>
</body></html>'''
r2 = fs.parse_html(HTML2, 'https://www.avito.ru/x/mebel/d_123', 2)
assert r2.price == '15000', r2.price
assert r2.title == 'Диван б/у'
assert r2.description.endswith('…') and len(r2.description) == 101, repr(r2.description)
print('[2] без JSON-LD, обрезка description до 100 символов + троеточие — OK')

# пустой/битый HTML не должен падать
r3 = fs.parse_html('<html></html>', 'https://www.avito.ru/x_1', 3)
assert r3.title == '' and r3.price == ''
print('[3] пустой HTML не роняет парсер — OK')
print('\nВСЕ ТЕСТЫ ПРОЙДЕНЫ')

# многоабзацное описание должно попасть в content целиком
HTML4 = '''<html><head><meta property="og:title" content="Тест"/></head><body>
<div data-marker="item-view/item-description">
  <p>Первый абзац.</p>
  <p>Второй абзац.</p>
  <div><span>Третий, вложенный.</span></div>
</div>
<div data-marker="item-view/item-address">Волгоград, ул. Мира, 5</div>
</body></html>'''
r4 = fs.parse_html(HTML4, 'https://www.avito.ru/x_9', 4)
print('\ncontent =', repr(r4.content))
assert 'Первый абзац' in r4.content, r4.content
assert 'Второй абзац' in r4.content, r4.content
assert 'Третий, вложенный' in r4.content, r4.content
assert r4.address == 'Волгоград, ул. Мира, 5', repr(r4.address)
assert r4.city == 'Волгоград', repr(r4.city)
print('[4] многоабзацное описание целиком + город из адреса — OK')


def test_mobile_variant():
    """Страница без og:-тегов: так выглядит мобильная выдача Avito.

    Здесь ломались сразу пять полей: description брало общесайтовое
    "Авито — Объявления на сайте Авито", image — иконку сайта, а
    category/city/address оставались пустыми."""
    page_html = (
        '<html><head>'
        '<meta name="description" content="Авито — Объявления на сайте Авито">'
        '<meta property="og:image" content="https://m.avito.ru/icons/touch-icon-512x512.png">'
        '</head><body>'
        '<div data-marker="item-view/item-description"><p>' + "Оп" * 80 + '</p></div>'
        '<img src="https://20.img.avito.st/image/1/photo.jpg">'
        '</body></html>')
    url = "https://www.avito.ru/volgograd/avtomobili/kia_sportage_2018_8329727762"
    listing = fs.parse_html(page_html, url, 7)
    assert listing.description.endswith("…"), listing.description
    assert "Авито — Объявления" not in listing.description
    assert listing.image == "https://20.img.avito.st/image/1/photo.jpg", listing.image
    assert listing.city == "Волгоград", listing.city
    assert listing.category == "Автомобили", listing.category
    print("[5] мобильный вариант страницы: описание, фото, город, категория — OK")


test_mobile_variant()


def test_breadcrumbs_and_photo():
    """Настоящая мобильная карточка: категория только в крошках, а на
    домене avito.st рядом с фотографиями лежат шрифты."""
    page_html = (
        '<html><head>'
        '<meta property="og:image" content="https://m.avito.ru/icons/touch-icon-512x512.png">'
        '<link rel="preload" href="https://www.avito.st/s/common/assets/fonts/regular.woff2">'
        '</head><body>'
        '<div data-marker="breadcrumbs">'
        '<span><a href="/">Авито</a></span>'
        '<span><a href="/volgograd">Волгоград</a></span>'
        '<span><a href="/volgograd/transport">Транспорт</a></span>'
        '<span><a href="/volgograd/avtomobili">Автомобили</a></span></div>'
        '<img src="https://20.img.avito.st/image/1/photo.jpg">'
        '</body></html>')
    listing = fs.parse_html(page_html, "https://www.avito.ru/x_1", 8)
    assert fs.breadcrumbs(page_html) == ["Авито", "Волгоград", "Транспорт", "Автомобили"]
    assert listing.category == "Автомобили", listing.category
    assert listing.city == "Волгоград", listing.city
    assert listing.image.endswith("photo.jpg"), listing.image
    print("[6] крошки дают категорию и город, шрифт не попадает в image — OK")


def test_no_city_from_stub_url():
    """Заглушка вместо ссылки не должна превращаться в город "Кс 1"."""
    from url_meta import category_from_url, city_from_url
    assert city_from_url("https://www.avito.ru/x_1") == ""
    assert category_from_url("https://www.avito.ru/x_1") == ""
    assert city_from_url("https://www.avito.ru/volgograd/avtomobili/kia_7654321098") == "Волгоград"
    print("[7] город из ссылки только когда ссылка настоящая — OK")


test_breadcrumbs_and_photo()
test_no_city_from_stub_url()


def test_auto_breadcrumbs_pick_section():
    """Крошки авто: последняя — поколение модели, а не категория."""
    crumbs = ["Иркутск", "Транспорт", "Автомобили", "Kia", "Sportage", "IV (2015—2018)"]
    from url_meta import category_from_crumbs
    assert category_from_crumbs(crumbs[1:]) == "Автомобили", category_from_crumbs(crumbs[1:])
    page_html = (
        '<div data-marker="breadcrumbs">'
        '<a href="/">Авито</a><a href="/x">…</a>'
        '<a href="/irkutsk/transport">Транспорт</a>'
        '<a href="/irkutsk/avtomobili">Автомобили</a>'
        '<a href="/irkutsk/avtomobili/kia">Kia</a>'
        '<a href="/irkutsk/avtomobili/kia/iv">IV (2015—2018)</a></div>'
        '<div data-marker="search-form/change-location">Иркутск</div>')
    listing = fs.parse_html(page_html, "https://www.avito.ru/x_1", 9)
    assert listing.category == "Автомобили", listing.category
    assert listing.city == "Иркутск", listing.city
    print("[8] поколение модели не подменяет категорию, «…» не подменяет город — OK")


test_auto_breadcrumbs_pick_section()


def test_photo_without_extension():
    """Фото Avito лежат на img.avito.st и расширения не имеют — рядом на
    том же домене лежат логотипы, шрифты и промо-значки."""
    photo = ("https://00.img.avito.st/image/1/"
             "1.GznOpra1t9DUB2nT9L4IH9wGtdBwDRnQpAS10g.YJM9xgdi5wjSFs7hbrow")
    page_html = (
        '<meta property="og:image" content="https://m.avito.ru/icons/touch-icon-512x512.png">'
        '<link href="https://www.avito.st/s/app/logo/180.png">'
        '<img src="https://avito.st/static/ims/86fec5d0_expensive_common_120x120.png">'
        f'<img src="{photo}">')
    assert fs.pick_image(page_html, {}) == photo, fs.pick_image(page_html, {})
    print("[9] фото без расширения находится, иконки и значки — нет — OK")


test_photo_without_extension()
