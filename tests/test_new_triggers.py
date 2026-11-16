from legal_sheaf.scanner_152fz import Compliance152FZScanner

def test_all_3_triggers():
    scanner = Compliance152FZScanner()
    
    # 1. Foreign auth
    h1 = '<script src="https://accounts.google.com/gsi/client"></script>'
    r1 = scanner.scan_html(h1)
    sigs1 = [v.signature for v in r1.foreign_transfer_violations]
    assert "foreign-auth-google-apple" in sigs1

    # 2. Pre-checked checkbox
    h2 = '<form><input name="phone"><input type="checkbox" checked> согласие на обработку данных <a href="/privacy">Политика</a></form>'
    r2 = scanner.scan_html(h2)
    sigs2 = [v.signature for f in r2.forms for v in f.violations]
    assert "prechecked-consent-checkbox" in sigs2

    # 3. Bundled offer
    h3 = '<form><input name="phone"><input type="checkbox"> согласие на обработку <a href="/oferta.pdf">Публичная оферта</a></form>'
    r3 = scanner.scan_html(h3)
    sigs3 = [v.signature for f in r3.forms for v in f.violations]
    assert "bundled-offer-consent-missing-separate-consent" in sigs3

    print("[SUCCESS] All 3 2025-2026 red triggers verified!")

if __name__ == "__main__":
    test_all_3_triggers()
