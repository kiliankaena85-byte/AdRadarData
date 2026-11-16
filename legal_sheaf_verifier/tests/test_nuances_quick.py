from legal_sheaf import Compliance152FZScanner, KoAPSanctionsCalculator

def test_telegram_widget():
    scanner = Compliance152FZScanner()
    html = '<script src="https://telegram.org/js/telegram-widget.js" data-telegram-login="mybot"></script>'
    r = scanner.scan_html(html)
    assert any(v.signature == "telegram-auth-widget" for v in r.foreign_transfer_violations)

def test_gtm_deep_inspect_benign():
    scanner = Compliance152FZScanner()
    html = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABCD123"></script>'
    containers = {"GTM-ABCD123": "/* local code */ var ym = 'https://mc.yandex.ru/metrika/tag.js';"}
    r = scanner.scan_html(html, gtm_containers=containers)
    assert len(r.foreign_transfer_violations) == 0

def test_gtm_deep_inspect_foreign():
    scanner = Compliance152FZScanner()
    html = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-EVIL999"></script>'
    containers = {"GTM-EVIL999": "/* tracker */ gtag('config', 'UA-12345-1'); google-analytics.com;"}
    r = scanner.scan_html(html, gtm_containers=containers)
    assert any(v.signature == "google-tag-manager" for v in r.foreign_transfer_violations)

def test_yandex_metrika_undisclosed():
    scanner = Compliance152FZScanner()
    html = '<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
    r = scanner.scan_html(html, policy_text="Мы собираем только контакты для связи.")
    assert any(v.signature == "yandex-metrika-undisclosed-in-policy" for v in r.foreign_transfer_violations)

def test_yandex_metrika_disclosed():
    scanner = Compliance152FZScanner()
    html = '<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
    policy = "На сайте используются файлы cookie и сервис веб-аналитики Яндекс.Метрика для сбора статистики."
    r = scanner.scan_html(html, policy_text=policy)
    assert not any(v.signature == "yandex-metrika-undisclosed-in-policy" for v in r.foreign_transfer_violations)

def test_bundled_pd_and_advertising_consent():
    scanner = Compliance152FZScanner()
    html = '''
    <form>
        <input type="text" name="phone">
        <label>
            <input type="checkbox">
            Согласен на обработку персональных данных и получение рекламных рассылок
        </label>
        <a href="/policy">Политика конфиденциальности</a>
    </form>
    '''
    r = scanner.scan_html(html)
    assert any(v.signature == "bundled-pd-and-advertising-consent" for f in r.forms for v in f.violations)
