"""
Unit Tests for Tensor (16, 36) Contract and Quality Gate 2.
Verifies shape (B, 16, 36), absence of NaNs/Infs, and valid parameter bounds
across 10 diverse HTML and plain-text documents.
"""

import pytest
import torch
import numpy as np

from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.schema import DocumentInput, SlotID


@pytest.fixture
def feature_builder():
    return SlotFeatureBuilder(device="cpu")


def create_10_diverse_documents() -> list[DocumentInput]:
    docs = []

    # 1. Empty document edge case
    docs.append(DocumentInput(
        clean_text="",
        raw_html="",
        page_url="",
    ))

    # 2. Minimal whitespace text
    docs.append(DocumentInput(
        clean_text="   \n\t   ",
        raw_html="<div>   </div>",
    ))

    # 3. Simple personal note / non-commercial text
    docs.append(DocumentInput(
        clean_text="Сегодня отличная погода в парке, гуляли с собакой два часа.",
        raw_html="<p>Сегодня отличная погода в парке, гуляли с собакой два часа.</p>",
    ))

    # 4. Longread (simulating 8000 words scientific/analytical essay)
    long_para = "В данном исследовании мы рассматриваем математические основания топологического анализа данных, клеточные пучки и когомологии на конечных комплексах. "
    long_text = long_para * 250
    docs.append(DocumentInput(
        clean_text=long_text,
        raw_html=f"<article><h1>Аналитический обзор</h1><p>{long_text[:500]}</p></article>",
        author_domain="math-journal.org",
    ))

    # 5. Full triad lawful marked advertisement
    docs.append(DocumentInput(
        clean_text=(
            "Реклама. ПАО Сбербанк, ИНН 7707083893, ОГРН 1027700132195, erid: 2VtzquXxxxx. "
            "Откройте вклад с доходностью до 16% годовых в мобильном приложении!"
        ),
        raw_html=(
            "<div class='ad-post'>"
            "<span>Реклама. ПАО Сбербанк, ИНН 7707083893, erid: 2VtzquXxxxx</span>"
            "<a href='https://sberbank.ru/vklad?erid=2VtzquXxxxx' class='btn'>Оформить вклад</a>"
            "</div>"
        ),
        page_url="https://sberbank.ru/vklad?erid=2VtzquXxxxx",
        author_domain="bank-news.ru",
    ))

    # 6. Unmarked advertisement with affiliate UTM link and promocode
    docs.append(DocumentInput(
        clean_text=(
            "Ребята, рекомендую лучший сервис для дизайнеров! "
            "По промокоду DESIGN скидка 40% на годовую подписку! "
            "Переходите и покупайте: https://design-pro.com/?ref=partner123&utm_source=tg"
        ),
        raw_html=(
            "<div class='post'>"
            "<p>Ребята, рекомендую <b>лучший сервис для дизайнеров</b>!</p>"
            "<a href='https://design-pro.com/?ref=partner123&utm_source=tg'>Купить со скидкой 40%</a>"
            "</div>"
        ),
        page_url="https://t.me/designer_blog/42",
        author_domain="t.me",
    ))

    # 7. Illegal surrogate label ("Партнерский материал" without "Реклама")
    docs.append(DocumentInput(
        clean_text=(
            "Партнерский материал. Сеть фитнес-клубов WorldFit объявляет распродажу абонементов. "
            "Цены от 9900 рублей. Успей забронировать до конца недели!"
        ),
        raw_html="<div class='sponsor'><h4>Партнерский материал</h4><p>WorldFit распродажа</p></div>",
    ))

    # 8. Multi-brand comparative table / review of 4 competitors
    docs.append(DocumentInput(
        clean_text=(
            "Сравнительный обзор 4 платформ: Яндекс Облако, VK Cloud, Selectel, SberCloud. "
            "Таблица характеристик: дисковая квота, стоимость за 1 ГБ, техподдержка. "
            "Каждое решение имеет свои преимущества и недостатки для разных задач."
        ),
        raw_html=(
            "<table>"
            "<tr><th>Провайдер</th><th>Цена</th><th>Плюсы</th></tr>"
            "<tr><td>Яндекс</td><td>1000</td><td>S3 хранилище</td></tr>"
            "<tr><td>VK</td><td>950</td><td>K8s кластеры</td></tr>"
            "<tr><td>Selectel</td><td>1100</td><td>Выделенные серверы</td></tr>"
            "</table>"
        ),
    ))

    # 9. 168-FZ Foreign words in commercial offer
    docs.append(DocumentInput(
        clean_text="MEGA SALE! BLACK FRIDAY! Best price, discounts up to 70%! Free shipping on all orders!",
        raw_html="<div class='hero'><h1>MEGA SALE</h1><button>BUY NOW</button></div>",
    ))

    # 10. Expired post (> 400 days old, KoAP Art. 4.5 1-year statute of limitations)
    docs.append(DocumentInput(
        clean_text="Старый пост со скидкой на курс программирования. Промокод OLD2022.",
        raw_html="<p>Старый пост</p>",
        published_days_ago=420,
    ))

    return docs


def test_quality_gate_2_tensor_shape_and_invariants(feature_builder):
    docs = create_10_diverse_documents()
    assert len(docs) == 10

    # Build batch tensor
    batch_tensor = feature_builder.build_batch_tensor(docs)
    
    # 1. Exact shape: (10, 16, 36)
    assert batch_tensor.shape == (10, 16, 36), f"Expected shape (10, 16, 36), got {batch_tensor.shape}"

    # 2. Strict numerical stability: NO NaNs, NO Infs
    assert not torch.isnan(batch_tensor).any(), "Found NaN in batch tensor!"
    assert not torch.isinf(batch_tensor).any(), "Found Inf in batch tensor!"

    # 3. Check Block A (0..7) telemetry coverage
    # Document 5 (Lawful marked ad) should have INN checksum score = 1.0 in Slot 6 and erid score = 1.0 in Slot 7
    doc5_tensor = batch_tensor[4]
    slot6_det = doc5_tensor[SlotID.ADVERTISER_INFO, 7].item()
    slot7_det = doc5_tensor[SlotID.ERID_INTEGRITY, 7].item()
    slot5_det = doc5_tensor[SlotID.AD_LABEL_MARK, 7].item()
    assert slot6_det == 1.0, f"Expected INN deterministic score 1.0 in Slot 6, got {slot6_det}"
    assert slot7_det == 1.0, f"Expected erid deterministic score 1.0 in Slot 7, got {slot7_det}"
    assert slot5_det == 1.0, f"Expected ad label deterministic score 1.0 in Slot 5, got {slot5_det}"

    # Document 7 (Surrogate label) should have -1.0 in Slot 5 Param 7
    doc7_tensor = batch_tensor[6]
    assert doc7_tensor[SlotID.AD_LABEL_MARK, 7].item() == -1.0

    # Document 9 (168-FZ) should trigger Slot 13
    doc9_tensor = batch_tensor[8]
    assert doc9_tensor[SlotID.RUSSIAN_LANG_168FZ, 7].item() == 1.0

    # Document 10 (published 420 days ago) should have 1.0 in Slot 15 Param 7 (statute of limitations)
    doc10_tensor = batch_tensor[9]
    assert doc10_tensor[SlotID.PROCEDURAL_KOAP, 7].item() == 1.0

    # 4. Check Block B (14..19, 6 dims) semantic projection
    semantic_block = batch_tensor[:, :, 14:20]
    assert semantic_block.shape == (10, 16, 6)
    assert not torch.all(semantic_block == 0.0)

    # 5. Check Block C (20..35, 16 dims) legal KB metrics
    legal_block = batch_tensor[:, :, 20:36]
    assert legal_block.shape == (10, 16, 16)
    # Check bounds for net margin (idx 32 -> 12 in Block C)
    net_margins = legal_block[:, :, 12]
    assert torch.all(net_margins >= -1.0)
    assert torch.all(net_margins <= 1.0)
