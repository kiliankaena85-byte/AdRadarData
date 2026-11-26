"""
Unit Tests for INN, OGRN, erid and 168-FZ Validators (Quality Gate 0).
"""

import pytest
from legal_sheaf.inn_erid_validators import (
    validate_inn_checksum,
    validate_ogrn_checksum,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    extract_erid_tokens,
    validate_erid_token_format,
    analyze_ad_label_status,
    detect_untranslated_foreign_words_168fz,
)


def test_inn_10_digit_validation():
    # Valid 10-digit INNs: Sberbank (7707083893), Yandex (7736207543)
    assert validate_inn_checksum("7707083893") is True
    assert validate_inn_checksum("7736207543") is True

    # Corrupted / invalid INNs
    assert validate_inn_checksum("7707083894") is False
    assert validate_inn_checksum("1234567890") is False
    assert validate_inn_checksum("770708389") is False  # too short
    assert validate_inn_checksum("77070838933") is False # 11 digits


def test_inn_12_digit_validation():
    # Known valid 12-digit INN
    valid_12 = "500100732259"
    assert validate_inn_checksum(valid_12) is True

    # Mutated invalid 12-digit INN
    assert validate_inn_checksum("500100732250") is False
    assert validate_inn_checksum("000000000000") is False


def test_ogrn_13_and_15_validation():
    # Sberbank 13-digit OGRN: 1027700132195
    assert validate_ogrn_checksum("1027700132195") is True
    # Invalid 13-digit OGRN
    assert validate_ogrn_checksum("1027700132190") is False

    # Valid 15-digit OGRNIP (30450011600003 % 13 = 8)
    valid_ogrnip = "304500116000038"
    assert validate_ogrn_checksum(valid_ogrnip) is True
    # Invalid 15-digit OGRNIP
    assert validate_ogrn_checksum("304500116000039") is False


def test_inn_extraction_from_text():
    sample_text = (
        "ООО 'Яндекс', ИНН 7736207543, ОГРН 1027700132195. "
        "Фейковый ИНН 1234567890 не должен быть найден!"
    )
    inns = find_valid_inns_in_text(sample_text)
    assert len(inns) == 1
    assert inns[0][0] == "7736207543"

    ogrns = find_valid_ogrns_in_text(sample_text)
    assert len(ogrns) == 1
    assert ogrns[0][0] == "1027700132195"


def test_erid_token_extraction():
    # Test valid erid in text
    text = "Специальный курс по праву. erid: 2VtzquXxxxx Реклама. ООО 'Вектор'"
    tokens = extract_erid_tokens(text)
    assert "2VtzquXxxxx" in tokens

    # Test erid in URL
    url = "https://example.com/course?utm_source=tg&erid=Kra23uYyyy"
    tokens_url = extract_erid_tokens("", page_url=url)
    assert "Kra23uYyyy" in tokens_url

    # Test erid in JSON metadata
    meta = {"ad_data": {"erid": "Pb3Xyz123456"}}
    tokens_meta = extract_erid_tokens("", platform_ad_meta=meta)
    assert "Pb3Xyz123456" in tokens_meta

    # Test invalid / trivial tokens rejected
    assert validate_erid_token_format("11111111") is False
    assert validate_erid_token_format("test") is False  # too short


def test_surrogate_ad_label_detection():
    # 1. Legal proper label: has exact word "Реклама"
    clean_legal = "Реклама. ООО 'Ромашка', ИНН 7707083893, erid: 2VtzquXxxxx"
    res1 = analyze_ad_label_status(clean_legal)
    assert res1["has_strict_ad_label"] is True
    assert res1["is_illegal_surrogate"] is False
    assert res1["status_code"] == 1.0

    # 2. Illegal surrogate: "Партнерский материал" without "Реклама"
    surrogate_text = "Партнерский материал. Скидки до 50% на все курсы школы!"
    res2 = analyze_ad_label_status(surrogate_text)
    assert res2["has_strict_ad_label"] is False
    assert res2["is_illegal_surrogate"] is True
    assert "партнерский материал" in res2["found_surrogates"]
    assert res2["status_code"] == -1.0


def test_168fz_untranslated_foreign_words():
    # Text with promotional Latin terms without translation
    promo_text = "Грандиозный Black Friday! Супер Sale до 70% и Free доставка по промокоду!"
    res = detect_untranslated_foreign_words_168fz(promo_text)
    assert res["has_violation_signals"] is True
    assert "black friday" in res["flagged_commercial_phrases"]
    assert "sale" in res["flagged_commercial_phrases"]
    assert "free" in res["flagged_commercial_phrases"]
    assert res["severity_score"] > 0.5

    # Neutral Russian text with whitelisted terms
    neutral_text = "Разработчик API опубликовал новую версию SDK на сайте в домене .ru"
    res_neutral = detect_untranslated_foreign_words_168fz(neutral_text)
    assert res_neutral["has_violation_signals"] is False


def test_vcru_sprint4_detector_guards():
    from legal_sheaf.inn_erid_validators import extract_commercial_targets

    # 1. Bearer-token prose like "loopback" must NOT be extracted as erid
    assert extract_erid_tokens("Сменить локальный bearer-токен loopback и перезапустить сервис.") == []
    assert validate_erid_token_format("loopback") is False  # pure alpha without digits

    # 2. Ordinary prose phrases like "рекламный бюджет" / "реклама курсов" are NOT ad markings
    res_prose = analyze_ad_label_status("В этом сезоне реклама или органика: мы увеличили рекламный бюджет. Реклама курсов дала лиды.")
    assert res_prose["has_strict_ad_label"] is False
    assert res_prose["is_illegal_surrogate"] is False

    # 3. Hashtag #реклама IS strict marking
    res_hash = analyze_ad_label_status("Дизайнерское освещение от студии #реклама")
    assert res_hash["has_strict_ad_label"] is True

    # 4. Hidden commercial targets extraction & neutral filtering
    targets = extract_commercial_targets(
        "Подробности в боте https://t.me/SovoKino_bot, пишите @andrey_dobrosvet, сайт svetville.ru, "
        "шаблон https://disk.yandex.ru/i/75rUGfQKDuJfig, профиль по номеру (79054105930 ) заблокирован.",
        raw_links=["https://vc.ru/services/12345"],
        page_url="https://vc.ru/services/12345",
    )
    assert "https://t.me/SovoKino_bot" in targets["messenger_links"]
    assert "@andrey_dobrosvet" in targets["at_handles"]
    assert "svetville.ru" in targets["text_domains"]
    assert not any("disk.yandex.ru" in u for u in targets["commercial_urls"])
    assert not targets["phones"]
    assert "https://vc.ru/services/12345" not in targets["commercial_urls"]
