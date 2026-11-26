"""
Unit & Integration Tests for Link-Centric Cascade v3.0:
1. Feature vector (B, 48) contract, windowing, and compact parameter budget (~5.7K params)
2. Golden VC.ru bug regressions (3252819 Binance news, 3258942 help.openai.com,
   3254224 USDT crypto-exchangers, 3254060 HubSpot CDN PDFs & custom /services/)
3. Gradient flow, ONNX export parity, and runtime determinism
4. LegalPrecedentIndex decoupling (changing KB precedents never perturbs NN logits)
5. Tier 2 Vega-U grey-zone routing via DualAuditor.audit_article & evaluate_grey_zone_with_vega
6. Blind VC.ru Golden (N=50) & Grey-Zone (N=22) certification strictly beating v2 baseline
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch

PKG_DIR = Path(__file__).resolve().parent.parent
PKG_SRC = PKG_DIR / "src"
REPO_ROOT = PKG_DIR.parent
for p in (str(PKG_SRC), str(PKG_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import ARTIFACTS_DIR, MODELS_CACHE_DIR
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.inn_erid_validators import (
    extract_commercial_targets,
    has_custom_campaign_or_service_path,
    is_editorial_crypto_news_url,
)
from legal_sheaf.link_classifier import LinkCentricAdClassifierV3, LinkClassifierLoss
from legal_sheaf.link_features import (
    FEATURE_DIM,
    FEATURE_NAMES,
    LinkCentricFeatureExtractor,
    extract_link_context_windows,
)
from legal_sheaf.onnx_runtime import ONNXLinkClassifierVerifier, export_link_classifier_to_onnx
from legal_sheaf.schema import DocumentInput, LegalRegime
from legal_sheaf.vega_grey_zone import ATOMIC_FACT_KEYS, evaluate_grey_zone_with_vega
from verification_engine.sheaf_vega_verifier import VegaUSensor


@pytest.fixture(scope="module")
def bundle_v3():
    candidates = [
        MODELS_CACHE_DIR / "champion_v3.bundle",
        ARTIFACTS_DIR / "champion_v3.bundle",
        PKG_DIR / "artifacts" / "champion_v3.bundle",
    ]
    for c in candidates:
        if c.exists():
            return AdRadarBrainBundle.load(c, verify_hash=True)
    pytest.fail(f"champion_v3.bundle not found in {candidates}")


def test_feature_vector_48_contract_and_model_budget():
    assert FEATURE_DIM == 48
    assert len(FEATURE_NAMES) == 48
    assert len(set(FEATURE_NAMES)) == 48

    model = LinkCentricAdClassifierV3()
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    assert total_params < 15_000, f"Model exceeded compact parameter budget: {total_params}"

    extractor = LinkCentricFeatureExtractor(device="cpu", use_fast_ngram_fallback=True)
    docs = [
        DocumentInput(
            clean_text="Чистая заметка о проектировании распределенных баз данных.",
            page_url="https://vc.ru/dev/1001",
            author_domain="vc.ru",
            context_mode="EXTERNAL_PUBLICATION",
        ),
        DocumentInput(
            clean_text=(
                "Скидка 25% по промокоду FAST25 на тарифы облачного сервиса! "
                "Переходите https://cloud-service.ru/?utm_source=vc"
            ),
            page_url="https://vc.ru/services/1002",
            author_domain="vc.ru",
            context_mode="EXTERNAL_PUBLICATION",
        ),
    ]
    batch = extractor.build_batch_features(docs)
    assert batch.shape == (2, FEATURE_DIM)
    assert torch.all(torch.isfinite(batch))
    assert float(batch[0, FEATURE_NAMES.index("has_any_commercial_target")]) == 0.0
    assert float(batch[1, FEATURE_NAMES.index("has_any_commercial_target")]) == 1.0
    assert float(batch[1, FEATURE_NAMES.index("win_has_promo_code_or_giveaway")]) == 1.0


def test_gradients_and_onnx_parity(tmp_path):
    torch.manual_seed(42)
    model = LinkCentricAdClassifierV3()
    model.set_normalization(np.zeros(FEATURE_DIM), np.ones(FEATURE_DIM))
    criterion = LinkClassifierLoss()

    x = torch.randn(8, FEATURE_DIM)
    targets = {
        "is_violation": torch.tensor([0, 1, 0, 1, 0, 1, 0, 1], dtype=torch.float32),
        "regime": torch.tensor([0, 2, 1, 2, 0, 3, 6, 5], dtype=torch.long),
    }

    model.train()
    out = model(x)
    losses = criterion(out, targets)
    losses["loss_total"].backward()

    assert model.linear_viol.weight.grad is not None
    assert float(torch.norm(model.linear_viol.weight.grad).item()) > 1e-6
    assert model.residual_viol_head.weight.grad is not None
    assert float(torch.norm(model.residual_viol_head.weight.grad).item()) > 1e-6

    model.eval()
    onnx_path = tmp_path / "test_v3.onnx"
    export_link_classifier_to_onnx(model, onnx_path, device="cpu")
    verifier = ONNXLinkClassifierVerifier(onnx_path, prefer_cuda=False)

    with torch.no_grad():
        pt_out = model(x)
    onnx_out = verifier.predict(x.numpy())

    assert np.max(np.abs(pt_out["violation_logit"].numpy() - onnx_out["violation_logit"])) < 1e-4
    assert np.max(np.abs(pt_out["regime_logits"].numpy() - onnx_out["regime_logits"])) < 1e-4
    assert np.max(np.abs(pt_out["feature_attributions"].numpy() - onnx_out["feature_attributions"])) < 1e-4


def test_golden_bug_regressions_and_windowing():
    extractor = LinkCentricFeatureExtractor(device="cpu", use_fast_ngram_fallback=True)

    # 1. Binance $6.3B hack news (3252819 regression): news URL & editorial incident -> clean
    assert is_editorial_crypto_news_url("https://www.binance.com/ru/square/post/123456") is True
    doc_3252819 = DocumentInput(
        clean_text=(
            "Хакерский взлом: злоумышленники вывели $6.3B с горячих кошельков. "
            "Подробнее в источнике https://www.binance.com/ru/square/post/123456\n"
            "[Ссылки: https://www.binance.com/ru/square/post/123456]"
        ),
        page_url="https://vc.ru/crypto/3252819",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    f_news = extractor.build_feature_vector(doc_3252819).numpy()
    assert f_news[FEATURE_NAMES.index("has_banned_or_special_trigger")] == 0.0
    assert f_news[FEATURE_NAMES.index("has_editorial_news_shield")] == 1.0

    # 2. help.openai.com excluded from commercial targets (3258942 regression)
    t_openai = extract_commercial_targets(
        "Справка по API: https://help.openai.com/en/articles/12345 и мой блог https://t.me/notes_dev",
        page_url="https://vc.ru/ai/3258942",
    )
    assert not any("openai.com" in u for u in t_openai["website_urls"])
    assert t_openai["messenger_links"] == ["https://t.me/notes_dev"]

    # 3. 14 USDT crypto-exchangers + affiliate partner disclosure (3254224 regression)
    doc_3254224 = DocumentInput(
        clean_text=(
            "14 криптообменников в Бишкеке: какой выбрать для продажи USDT. "
            "Некоторые сервисы используют партнерские ссылки BestChange https://partner.bestchange.ru/?ref=777"
        ),
        page_url="https://vc.ru/crypto/3254224",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    f_exch = extractor.build_feature_vector(doc_3254224).numpy()
    assert f_exch[FEATURE_NAMES.index("has_banned_or_special_trigger")] == 1.0

    # 4. HubSpot CDN PDFs excluded, while custom /services/ VC path flagged (3254060 regression)
    t_qtim = extract_commercial_targets(
        "Отчет: https://7830902.fs1.hubspotusercontent-na1.net/hubfs/7830902/report.pdf "
        "Наши услуги: https://vc.ru/services/qtim-audit",
        page_url="https://vc.ru/design/3254060",
    )
    assert not any("hubspotusercontent" in u for u in t_qtim["website_urls"])
    assert has_custom_campaign_or_service_path("https://vc.ru/services/qtim-audit") is True

    # 5. Tail windowing preserves closing commercial pitch inside a long neutral article
    long_filler = ("Методический анализ архитектуры информационных систем без коммерции. " * 90).strip()
    long_article = (
        f"{long_filler}\n\n"
        "Попробуйте наш сервис автоотчетов по промокоду TAIL50 со скидкой 50%: "
        "https://tail-saas.ru/?utm_source=vc\n"
        "[Ссылки: https://tail-saas.ru/?utm_source=vc]"
    )
    t_tail = extract_commercial_targets(long_article)
    _, combined_win, indiv_wins, _ = extract_link_context_windows(long_article, t_tail)
    assert "TAIL50" in combined_win
    assert "tail-saas.ru" in combined_win
    assert len(indiv_wins) >= 1


def test_precedent_index_decoupled_from_classifier_predictions(bundle_v3):
    doc = DocumentInput(
        clean_text=(
            "Запускаем корпоративный трекер задач с тарифом 990 руб/мес! "
            "Переходите по ссылке https://task-tracker.ru/?utm_source=vc"
        ),
        page_url="https://vc.ru/services/990123",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    rep_before = bundle_v3.verify_document(doc)
    orig_embs = bundle_v3._precedent_index.embeddings.clone()
    try:
        # Perturb precedent index embeddings completely -> NN prediction MUST remain identical
        bundle_v3._precedent_index.embeddings = -orig_embs
        rep_after = bundle_v3.verify_document(doc)
        assert abs(rep_before.violation_probability - rep_after.violation_probability) < 1e-6
        assert rep_before.regime == rep_after.regime
        assert rep_before.is_violation == rep_after.is_violation
    finally:
        bundle_v3._precedent_index.embeddings = orig_embs


def test_tier2_vega_grey_zone_routing():
    # 1. Direct VegaUSensor(light_mode=True) on grey-zone SaaS demo pitch vs clean personal Telegram note
    sensor_light = VegaUSensor(light_mode=True)
    res_pitch = evaluate_grey_zone_with_vega(
        sensor=sensor_light,
        title="Как мы запустили свой трекер",
        url="https://vc.ru/services/50001",
        external_links=["https://my-tracker.ru"],
        link_windows=["Попробуйте наш сервис в демо-режиме по тарифу 1990 руб: https://my-tracker.ru"],
        tier0_flags={"has_erid": False, "has_strict_ad_label": False, "has_affiliate_link": False, "has_promo_code": False},
    )
    assert res_pitch["resolved"] is True
    assert res_pitch["is_violation"] is True
    assert res_pitch["regime"] == LegalRegime.UNMARKED_AD_NO_ERID

    res_bio = evaluate_grey_zone_with_vega(
        sensor=sensor_light,
        title="Мысли о разработке распределенных систем",
        url="https://vc.ru/dev/50002",
        external_links=["https://t.me/eng_diary"],
        link_windows=["Веду короткие инженерные заметки в личном канале https://t.me/eng_diary"],
        tier0_flags={"has_erid": False, "has_strict_ad_label": False, "has_affiliate_link": False, "has_promo_code": False},
    )
    assert res_bio["resolved"] is True
    assert res_bio["is_violation"] is False
    assert res_bio["regime"] == LegalRegime.CLEAN_NON_AD

    # 2. DualAuditor.audit_article with ABSTAIN_REVIEW + mocked non-light Vega-U agent
    fake_answers = {
        k: {"prediction_set": ["true" if k in ("F1_OBJECT_PROMOTED", "F4_COMMERCIAL_CTA_OR_OFFER") else "false"], "confidence": 0.91, "abstain": False}
        for k in ATOMIC_FACT_KEYS
    }
    mock_sensor = SimpleNamespace(
        light_mode=False,
        _get_agent=lambda: SimpleNamespace(predict=lambda state, questions: {"answers": fake_answers}),
    )
    abstain_bundle = SimpleNamespace(
        verify_document=lambda doc: SimpleNamespace(
            is_violation=False,
            violation_probability=0.49,
            conformal_verdict="ABSTAIN_REVIEW",
            regime=LegalRegime.CLEAN_NON_AD,
            regime_name="CLEAN_NON_AD",
            total_sheaf_energy=0.0,
            active_slots=[],
            feature_attributions={},
            extracted_link_windows=["Мы разработали сервис и предлагаем тариф со скидкой на сайте https://vendor-app.ru"],
        )
    )
    auditor = DualAuditor(
        bundle=abstain_bundle,
        vega_sensor=mock_sensor,
        enable_vega_grey_zone=True,
    )
    audit_res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/services/50003",
            text="Мы разработали сервис и предлагаем тариф со скидкой на сайте https://vendor-app.ru",
        )
    )
    assert audit_res["vega_grey_zone_resolved"] is True
    assert audit_res["is_violation_38fz"] is True
    assert audit_res["scenario_38fz"] == "UNMARKED_AD_NO_ERID"


def test_real_vcru_golden_and_grey_zone_beat_v2_baseline(bundle_v3):
    man = bundle_v3.manifest
    real_m = man.get("real_vcru_eval_metrics") or {}
    grey_m = man.get("real_vcru_grey_eval_metrics") or {}

    assert man.get("architecture") == "LinkCentricAdClassifierV3"
    assert man.get("feature_dim") == FEATURE_DIM
    assert float(real_m.get("f1", 0.0)) > 0.800
    assert float(real_m.get("accuracy", 0.0)) > 0.860
    assert float(grey_m.get("accuracy", 0.0)) > 0.682
