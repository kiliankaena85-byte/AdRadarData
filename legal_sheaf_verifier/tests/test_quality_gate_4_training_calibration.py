"""
Quality Gate 4 Test: Verification of Trained LinkCentricAdClassifierV3 and Conformal Calibration.
Evaluates:
- Macro-F1 across 7 regimes >= 0.93
- Precision on violation class after conformal threshold >= 0.96
- Expected Calibration Error (ECE) <= 0.05
- Feature attributions non-degenerate & normalization buffers fitted
"""

import json
import sys
from pathlib import Path

# Add project root and src to sys.path
pkg_root = Path(__file__).resolve().parent.parent
if str(pkg_root) not in sys.path:
    sys.path.insert(0, str(pkg_root))
if str(pkg_root / "src") not in sys.path:
    sys.path.insert(0, str(pkg_root / "src"))

import numpy as np
import pytest
import torch

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.calibration import ConformalCalibrator, compute_ece
from legal_sheaf.inn_erid_validators import extract_commercial_targets
from legal_sheaf.link_classifier import LinkCentricAdClassifierV3
from legal_sheaf.link_features import FEATURE_DIM, FEATURE_NAMES, LinkCentricFeatureExtractor
from legal_sheaf.schema import DocumentInput
from training.procedural_case_sim import ProceduralCaseSimulator
from training.train_link_classifier import build_feature_tensors_from_cases, compute_macro_f1


@pytest.fixture
def artifacts_dir():
    pkg_root = Path(__file__).resolve().parent.parent
    return pkg_root / "artifacts"


def test_quality_gate_4_metrics(artifacts_dir):
    model_path = artifacts_dir / "link_classifier_v3.pt"
    calib_path = artifacts_dir / "conformal_calibration.json"

    assert model_path.exists(), f"Model checkpoint not found at {model_path}"
    assert calib_path.exists(), f"Calibration file not found at {calib_path}"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load model
    model = LinkCentricAdClassifierV3().to(device)
    ckpt = torch.load(model_path, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    ckpt_metrics = ckpt.get("metrics") or {}
    use_ngram = bool(ckpt_metrics.get("use_fast_ngram_fallback", False))

    # Load calibrator
    calibrator = ConformalCalibrator()
    calibrator.load(str(calib_path))

    # Evaluate on 170 statutory holdout cases using exact encoder mode
    sim = ProceduralCaseSimulator(seed=9999)
    test_cases = sim.generate_dataset(num_samples=170)
    extractor = LinkCentricFeatureExtractor(device=str(device), use_fast_ngram_fallback=use_ngram)
    X_test, y_viol_te, y_reg_te = build_feature_tensors_from_cases(test_cases, extractor)

    with torch.no_grad():
        x = X_test.to(device)
        preds = model(x)
        v_logits = preds["violation_logit"].squeeze(-1).cpu().numpy()
        reg_logits = preds["regime_logits"].cpu().numpy()
        attributions = preds["feature_attributions"].cpu().numpy()

    labels = y_viol_te.numpy()
    reg_labels = y_reg_te.numpy()

    # 1. Macro-F1 across 7 regimes >= 0.93
    pred_regimes = np.argmax(reg_logits, axis=-1)
    macro_f1 = compute_macro_f1(pred_regimes, reg_labels, num_classes=7)
    assert macro_f1 >= 0.93, f"Macro-F1 {macro_f1:.4f} is below 0.93!"

    # 2. Precision on violation class >= 0.96
    calib_logits = v_logits / calibrator.temperature_val
    probs = 1.0 / (1.0 + np.exp(-calib_logits))
    pred_viol = (probs >= calibrator.decision_threshold).astype(int)
    pos_mask = (pred_viol == 1)
    assert np.sum(pos_mask) > 0
    precision = float(np.mean(labels[pos_mask]))
    assert precision >= 0.96, f"Precision {precision:.4f} is below 0.96!"

    # 3. Expected Calibration Error (ECE) <= 0.05
    ece = compute_ece(probs, labels)
    assert ece <= 0.05, f"ECE {ece:.4f} exceeds 0.05!"

    # 4. Valid normalization buffers & finite non-trivial attributions
    assert attributions.shape == (len(test_cases), FEATURE_DIM)
    assert np.all(np.isfinite(attributions))
    assert float(np.max(np.abs(attributions))) > 1e-4
    assert float(model.feat_std.min().item()) >= 1e-2


def test_real_vcru_golden_set_loading_and_honest_eval(artifacts_dir):
    golden_path = Path(__file__).resolve().parent / "golden_set_real_vcru.jsonl"
    assert golden_path.exists(), f"Missing real VC.ru golden set: {golden_path}"

    rows = [json.loads(line) for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 12, f"Expected >= 12 real VC.ru examples, found {len(rows)}"

    regimes = {r["expected_regime"] for r in rows}
    assert "UNMARKED_AD_NO_ERID" in regimes
    assert "CLEAN_NON_AD" in regimes
    assert "CLEAN_MARKED_AD" in regimes

    bundle_path = artifacts_dir / "champion_v3.bundle"
    assert bundle_path.exists(), f"Missing champion_v3.bundle at {bundle_path}"
    bundle = AdRadarBrainBundle.load(bundle_path, verify_hash=True)

    man = bundle.manifest
    assert man.get("architecture") == "LinkCentricAdClassifierV3"
    assert man.get("feature_dim") == FEATURE_DIM
    assert man.get("encoder_model") == "deepvk/USER2-base"
    assert man.get("use_fast_ngram_fallback") is False
    assert man.get("embeddings_dim") == 768
    assert isinstance(man.get("synthetic_eval_metrics"), dict)
    assert isinstance(man.get("real_vcru_eval_metrics"), dict)
    assert isinstance(man.get("real_vcru_grey_eval_metrics"), dict)
    assert int(man.get("real_human_labeled_count", 0)) == len(rows)

    probs, preds, labels = [], [], []
    case_3255318_pred = None
    for row in rows:
        assert "post_id" in row and "text" in row and "expected_is_violation" in row
        doc = DocumentInput(
            clean_text=row["text"],
            page_url=row["url"],
            author_domain=row.get("author_domain", "vc.ru"),
            platform_ad_meta=row.get("platform_ad_meta") or {},
            context_mode="EXTERNAL_PUBLICATION",
        )
        rep = bundle.verify_document(doc)
        assert 0.0 <= rep.violation_probability <= 1.0
        probs.append(float(rep.violation_probability))
        preds.append(int(bool(rep.is_violation)))
        labels.append(int(bool(row["expected_is_violation"])))
        if int(row["post_id"]) == 3255318:
            case_3255318_pred = bool(rep.is_violation)

    assert case_3255318_pred is True, "Verified CaseGuru 3255318 must be flagged as violation"

    probs_arr = np.array(probs, dtype=np.float32)
    preds_arr = np.array(preds, dtype=np.int32)
    labels_arr = np.array(labels, dtype=np.int32)
    observed_acc = round(float(np.mean(preds_arr == labels_arr)), 4)
    observed_brier = round(float(np.mean((probs_arr - labels_arr) ** 2)), 4)

    real_m = man["real_vcru_eval_metrics"]
    assert abs(float(real_m["accuracy"]) - observed_acc) <= 1e-4
    assert abs(float(real_m["brier_score"]) - observed_brier) <= 1e-4
    assert int(real_m["sample_count"]) == len(rows)


def test_vcru_labeler_ui_and_url_bracket_fix(tmp_path):
    # 1. URL regex must not swallow trailing closing square bracket ']'
    matched_urls = extract_commercial_targets(
        "[Ссылки: https://makoo.ai/?ref=abc https://wuun.ru/ARNgXJv]"
    )["commercial_urls"]
    assert matched_urls == ["https://makoo.ai/?ref=abc", "https://wuun.ru/ARNgXJv"]

    # 2. Offline labeler HTML generator produces self-contained 0-CDN UI with 4 hotkeys
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.label_vcru_ui import generate_labeler_page

    sample_in = tmp_path / "vcru_to_label.jsonl"
    sample_in.write_text(
        json.dumps(
            {
                "post_id": 999111,
                "url": "https://vc.ru/test/999111",
                "title": "Тестовая статья",
                "text_window": "Текст тестовой статьи для разметки.",
                "external_links": ["https://example.ru/?ref=123"],
                "is_promo": False,
                "is_editorial": True,
                "erids": [],
                "has_strict_ad_label": False,
                "model_verdict": {"regime": "CLEAN_NON_AD", "violation_probability": 0.1},
                "human_label": None,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "labeler.html"
    res = generate_labeler_page(input_jsonl=sample_in, output_html=out_html)
    assert res.exists()
    content = res.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in content
    assert "cdnjs.cloudflare.com" not in content
    assert "UNMARKED_AD_NO_ERID" in content
    assert "CLEAN_MARKED_AD" in content
    assert "CLEAN_NON_AD" in content
    assert "SKIP" in content
    assert "vcru_human_labeled.jsonl" in content
    assert "new Blob" in content


def test_sprint4_head_consistency_no_channel_and_crypto_news(artifacts_dir):
    from legal_sheaf.schema import LegalRegime

    bundle = AdRadarBrainBundle.load(artifacts_dir / "champion_v3.bundle", verify_hash=True)
    safe_regimes = {
        LegalRegime.CLEAN_NON_AD,
        LegalRegime.CLEAN_MARKED_AD,
        LegalRegime.OVER_MARKED_NON_AD,
    }

    # 1. NO_COMMERCIAL_CHANNEL invariant on external VC.ru essay without links/contacts/promo
    doc_clean = DocumentInput(
        clean_text="Четыре рождения технического института: история образования, науки и людей без ссылок и рекламы курсов.",
        page_url="https://vc.ru/life/3259241",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    rep_clean = bundle.verify_document(doc_clean)
    assert rep_clean.is_violation is False
    assert rep_clean.regime == LegalRegime.CLEAN_NON_AD
    assert rep_clean.violation_probability <= 0.15

    # 2. Crypto market news without exchange affiliate link or buy CTA -> banned trigger = 0 & CLEAN_NON_AD
    doc_crypto = DocumentInput(
        clean_text="Обзор рынка: биткоин на OKX доходил до $62 000, ставки финансирования снизились. Это не инвестиционная рекомендация.",
        page_url="https://vc.ru/crypto/3259252",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    feat_crypto = bundle._feature_builder.build_feature_vector(doc_crypto).cpu().numpy()
    assert feat_crypto[FEATURE_NAMES.index("has_banned_or_special_trigger")] == 0.0
    assert feat_crypto[FEATURE_NAMES.index("has_editorial_news_shield")] == 1.0
    rep_crypto = bundle.verify_document(doc_crypto)
    assert rep_crypto.is_violation is False
    assert rep_crypto.regime == LegalRegime.CLEAN_NON_AD

    # 3. Strict Head Consistency across all real golden VC.ru cases
    golden_path = Path(__file__).resolve().parent / "golden_set_real_vcru.jsonl"
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rep = bundle.verify_document(
            DocumentInput(
                clean_text=row["text"],
                page_url=row["url"],
                author_domain=row.get("author_domain", "vc.ru"),
                platform_ad_meta=row.get("platform_ad_meta") or {},
                context_mode="EXTERNAL_PUBLICATION",
            )
        )
        if rep.is_violation:
            assert rep.regime not in safe_regimes, f"Contradiction on {row['post_id']}: viol=True, reg={rep.regime}"
        else:
            assert rep.regime in safe_regimes, f"Contradiction on {row['post_id']}: viol=False, reg={rep.regime}"


def test_slot_08_slot_10_marking_consistency_and_anti_leakage(tmp_path):
    from legal_sheaf.explainer import LegalSheafExplainer
    from legal_sheaf.schema import LegalRegime
    from training.train_link_classifier import _load_safe_real_vcru_cases

    extractor = LinkCentricFeatureExtractor(device="cpu", use_fast_ngram_fallback=True)

    # 1. Own-website & Multi-brand shield on UGC platform (AUTO mode, zero links, lowercase quotes)
    doc_ugc = DocumentInput(
        clean_text="Личные заметки: «погулять по сайту» и «пусть разгонится» без коммерции.",
        page_url="https://vc.ru/life/999001",
        author_domain="vc.ru",
        context_mode="AUTO",
    )
    t_ugc = extractor.build_feature_vector(doc_ugc).cpu().numpy()
    assert t_ugc[FEATURE_NAMES.index("is_own_website")] == 0.0
    assert t_ugc[FEATURE_NAMES.index("is_uniform_multibrand_shield")] == 0.0

    # 2. Uniform multi-brand shield triggers on >= 3 unique domains
    doc_domains = DocumentInput(
        clean_text="Сравнение серверов: alpha-cloud.ru, beta-host.ru и gamma-vps.ru.",
        page_url="https://habr.com/ru/articles/111/",
        author_domain="habr.com",
        context_mode="EXTERNAL_PUBLICATION",
    )
    t_dom = extractor.build_feature_vector(doc_domains).cpu().numpy()
    assert t_dom[FEATURE_NAMES.index("is_own_website")] == 0.0
    assert t_dom[FEATURE_NAMES.index("is_uniform_multibrand_shield")] == 1.0

    # 3. MARKING_CONSISTENCY forbids CLEAN_MARKED_AD when ERID == 0 and platform_ad_meta is empty
    explainer = LegalSheafExplainer()
    logits_clean_marked = torch.zeros((1, 7))
    logits_clean_marked[0, LegalRegime.CLEAN_MARKED_AD.value] = 8.0
    fake_out = {
        "violation_logit": torch.tensor([[-3.0]]),
        "regime_logits": logits_clean_marked,
        "feature_attributions": torch.zeros((1, FEATURE_DIM)),
    }
    doc_no_erid_promo = DocumentInput(
        clean_text="Сервис аналитики со скидкой 30%: https://promo-shop.ru/?ref=123",
        page_url="https://vc.ru/services/999002",
        author_domain="vc.ru",
        platform_ad_meta={},
        context_mode="EXTERNAL_PUBLICATION",
    )
    rep_mc = explainer.explain(doc=doc_no_erid_promo, model_output=fake_out)
    assert rep_mc.regime != LegalRegime.CLEAN_MARKED_AD
    assert rep_mc.is_violation is True
    assert rep_mc.regime == LegalRegime.UNMARKED_AD_NO_ERID

    # 4. Anti-leakage filter in train_link_classifier excludes golden_set post_id (e.g. 3255318)
    real_jsonl = tmp_path / "vcru_human_labeled.jsonl"
    real_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"post_id": 3255318, "url": "https://vc.ru/marketing/3255318", "text_window": "CaseGuru", "human_label": "UNMARKED_AD_NO_ERID"}),
                json.dumps({"post_id": 8880001, "url": "https://vc.ru/life/8880001", "text_window": "Чистый текст без ссылок", "human_label": "CLEAN_NON_AD"}),
                json.dumps({"post_id": 8880002, "url": "https://vc.ru/services/8880002", "text_window": "Промо https://shop.ru/?ref=1", "human_label": "UNMARKED_AD_NO_ERID"}),
                json.dumps({"post_id": 8880003, "url": "https://vc.ru/life/8880003", "text_window": "Пропуск", "human_label": "SKIP"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tr_w, cal_w, stats = _load_safe_real_vcru_cases(str(real_jsonl), oversample_factor=3)
    assert stats["filtered_golden_overlap"] == 1
    assert stats["safe_real_unique"] == 2
    assert len(tr_w) == 6  # weight 3x
    assert all("3255318" not in d.page_url for d, _ in tr_w + cal_w)
