"""
Unit & Integration Tests for the 5-Tier Orthogonal Ensemble Labeler,
Anti-Echo-Chamber Rule, 5-Second Micro-Question Generator, Chat Batch Export/Import,
Zero-CDN Micro-Question Desk v2 (labeler.html), and Weighted Train/Cal Loader.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PKG_DIR.parent
if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from legal_sheaf.schema import LegalRegime
from scripts.collect_vcru_labeling_set import load_golden_exclusions
from scripts.ensemble_labeler import (
    arbitrate_ensemble_votes,
    build_micro_question,
    build_tier0_passport,
    derive_regime_from_atomic_facts,
    evaluate_tier1a_sheaf,
    export_chat_batches,
    export_clean_training_jsonl,
    import_chat_batch_json,
    load_jury_models_config,
)
from scripts.label_vcru_ui import generate_labeler_page
from training.train_link_classifier import _load_safe_real_vcru_cases


def test_jury_models_config_zero_hardcode_and_free_lock():
    cfg = load_jury_models_config()
    assert len(cfg["tier_2b_nvidia"]) >= 1
    assert len(cfg["tier_2c_independent"]) >= 1
    assert cfg["tier_2b_nvidia"][0] != cfg["tier_2c_independent"][0]
    for m in cfg["tier_2b_nvidia"] + cfg["tier_2c_independent"]:
        assert m.endswith(":free") or m == "openrouter/free"


def test_statutory_formula_derive_regime_from_atomic_facts():
    # 1. Official marked ad with erid + strict ad label -> CLEAN_MARKED_AD
    t0_marked = {
        "has_erid": True,
        "has_strict_ad_label": True,
        "is_promo": True,
        "has_affiliate_link": True,
        "has_promo_code": False,
        "commercial_targets": ["https://example.ru/?erid=2Vtzqv6aaRy"],
        "external_links": ["https://example.ru/?erid=2Vtzqv6aaRy"],
    }
    viol, reg = derive_regime_from_atomic_facts(
        t0_marked,
        {"F1": True, "F2": False, "F3": False, "F4": True, "F5": False},
    )
    assert viol is False
    assert reg == LegalRegime.CLEAN_MARKED_AD

    # 2. Author personal blog link at end of analytical post without offer -> CLEAN_NON_AD
    t0_bio = {
        "has_erid": False,
        "has_strict_ad_label": False,
        "is_promo": False,
        "has_affiliate_link": False,
        "has_promo_code": False,
        "commercial_targets": ["https://t.me/author_notes"],
        "external_links": ["https://t.me/author_notes"],
    }
    viol2, reg2 = derive_regime_from_atomic_facts(
        t0_bio,
        {"F1": False, "F2": True, "F3": False, "F4": False, "F5": False},
    )
    assert viol2 is False
    assert reg2 == LegalRegime.CLEAN_NON_AD

    # 3. Unmarked native promo with promo code and UTM link -> UNMARKED_AD_NO_ERID
    t0_unmarked = {
        "has_erid": False,
        "has_strict_ad_label": False,
        "is_promo": False,
        "has_affiliate_link": True,
        "has_promo_code": True,
        "commercial_targets": ["https://shop.example.ru/?utm_campaign=VC10"],
        "external_links": ["https://shop.example.ru/?utm_campaign=VC10"],
    }
    viol3, reg3 = derive_regime_from_atomic_facts(
        t0_unmarked,
        {"F1": True, "F2": False, "F3": False, "F4": True, "F5": False},
    )
    assert viol3 is True
    assert reg3 == LegalRegime.UNMARKED_AD_NO_ERID

    # 4. Banned/special regime -> BANNED_OR_SPECIAL_REGIME
    viol4, reg4 = derive_regime_from_atomic_facts(
        t0_unmarked,
        {"F1": True, "F2": False, "F3": False, "F4": True, "F5": True},
    )
    assert viol4 is True
    assert reg4 == LegalRegime.BANNED_OR_SPECIAL_REGIME


def test_anti_echo_chamber_blocks_sheaf_self_confirmation():
    rec = {
        "post_id": 9990001,
        "url": "https://vc.ru/crypto/9990001-razbor",
        "title": "Разбор макроэкономики и цифр недели",
        "paragraphs": [
            "В этом обзоре разберем динамику ставок ЦБ за квартал.",
            "Это не инвестиционная рекомендация, короткие заметки веду в канале https://t.me/NeonDropscrypto",
        ],
        "full_text": "Разбор макроэкономики. Короткие заметки веду в канале https://t.me/NeonDropscrypto",
        "external_links": ["https://t.me/NeonDropscrypto"],
        "commercial_targets": ["https://t.me/NeonDropscrypto"],
    }
    t0 = build_tier0_passport(rec)

    # Case A: Only SheafNet + Vega-U agree (0 external cloud models) -> MUST NOT become AUTO_GOLD!
    local_only_votes = [
        {
            "voter": "LinkCentric-V3",
            "family": "link_centric_local",
            "facts": {"F1_OBJECT_PROMOTED": False, "F2_AUTHOR_SELF_BIO": True, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": False,
            "regime": "CLEAN_NON_AD",
            "confidence": 0.96,
        },
        {
            "voter": "Laya-Vega-U",
            "family": "vega_nli_local",
            "facts": {"F1_OBJECT_PROMOTED": False, "F2_AUTHOR_SELF_BIO": True, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": False,
            "regime": "CLEAN_NON_AD",
            "confidence": 0.94,
            "abstain": False,
        },
    ]
    res_local = arbitrate_ensemble_votes(rec, t0, local_only_votes)
    assert res_local["ensemble_status"] == "NEEDS_MICRO_Q"

    # Case B: 3 vs 1 disagreement -> MUST route to NEEDS_MICRO_Q with <= 300 char quote
    split_votes = local_only_votes + [
        {
            "voter": "nemotron-3-ultra",
            "family": "nvidia_moe",
            "facts": {"F1_OBJECT_PROMOTED": False, "F2_AUTHOR_SELF_BIO": True, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": False,
            "regime": "CLEAN_NON_AD",
            "confidence": 0.95,
        },
        {
            "voter": "gemma-4-31b",
            "family": "independent_devils_advocate",
            "facts": {"F1_OBJECT_PROMOTED": True, "F2_AUTHOR_SELF_BIO": False, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": True, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": True,
            "regime": "UNMARKED_AD_NO_ERID",
            "confidence": 0.91,
        },
    ]
    res_split = arbitrate_ensemble_votes(rec, t0, split_votes)
    assert res_split["ensemble_status"] == "NEEDS_MICRO_Q"
    mq = res_split["micro_question"]
    assert mq is not None
    assert len(mq["quote_snippet"]) <= 300
    assert "t.me/NeonDropscrypto" in mq["quote_snippet"]
    assert mq["option_1"]["resulting_regime"] != mq["option_2"]["resulting_regime"]

    # Case C: Unanimous 4/4 across Sheaf + Vega-U + 2 independent families with conf >= 0.90 -> AUTO_GOLD (weight=2.0)
    unanimous_votes = local_only_votes + [
        {
            "voter": "nemotron-3-ultra",
            "family": "nvidia_moe",
            "facts": {"F1_OBJECT_PROMOTED": False, "F2_AUTHOR_SELF_BIO": True, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": False,
            "regime": "CLEAN_NON_AD",
            "confidence": 0.95,
        },
        {
            "voter": "gemma-4-31b",
            "family": "independent_devils_advocate",
            "facts": {"F1_OBJECT_PROMOTED": False, "F2_AUTHOR_SELF_BIO": True, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": False,
            "regime": "CLEAN_NON_AD",
            "confidence": 0.93,
        },
    ]
    res_gold = arbitrate_ensemble_votes(rec, t0, unanimous_votes)
    assert res_gold["ensemble_status"] == "AUTO_GOLD"
    assert res_gold["consensus_regime"] == "CLEAN_NON_AD"
    assert res_gold["sample_weight"] == 2.0


def test_chat_batches_import_ui_and_weighted_loader(tmp_path: Path):
    ens_path = tmp_path / "vcru_ensemble_pool.jsonl"
    rec_auto = {
        "post_id": 888001,
        "url": "https://vc.ru/ai/888001-clean-news",
        "rubric": "ai",
        "title": "Вышел новый открытый стандарт компиляторов",
        "text_window": "Инженерный разбор изменений в стандарте без ссылок и коммерции.",
        "full_text": "Инженерный разбор изменений в стандарте без ссылок и коммерции.",
        "external_links": [],
        "commercial_targets": [],
        "erids": [],
        "has_strict_ad_label": False,
        "is_promo": False,
        "ensemble": {
            "ensemble_status": "AUTO_GOLD",
            "consensus_regime": "CLEAN_NON_AD",
            "consensus_is_violation": False,
            "consensus_confidence": 0.95,
            "sample_weight": 2.0,
            "label_source": "UNANIMOUS_CONSENSUS",
            "votes": [],
            "micro_question": None,
        },
    }
    t0_disp = build_tier0_passport(
        {
            "post_id": 888002,
            "url": "https://vc.ru/marketing/888002-promo-case",
            "title": "Как мы ускорили отдел продаж с CRM-ботом",
            "full_text": "Подробный кейс внедрения. По промокоду START20 скидка 20% в боте https://t.me/crm_helper_bot",
            "external_links": ["https://t.me/crm_helper_bot"],
            "commercial_targets": ["https://t.me/crm_helper_bot"],
        }
    )
    votes_disp = [
        {
            "voter": "LinkCentric-V3",
            "family": "link_centric_local",
            "facts": {"F1_OBJECT_PROMOTED": True, "F2_AUTHOR_SELF_BIO": False, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": True, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": True,
            "regime": "UNMARKED_AD_NO_ERID",
            "confidence": 0.94,
        },
        {
            "voter": "Laya-Vega-U",
            "family": "vega_nli_local",
            "facts": {"F1_OBJECT_PROMOTED": True, "F2_AUTHOR_SELF_BIO": False, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": True, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": True,
            "regime": "UNMARKED_AD_NO_ERID",
            "confidence": 0.92,
            "abstain": False,
        },
        {
            "voter": "nemotron-3-ultra",
            "family": "nvidia_moe",
            "facts": {"F1_OBJECT_PROMOTED": True, "F2_AUTHOR_SELF_BIO": False, "F3_UNIFORM_MULTI_REVIEW": False, "F4_COMMERCIAL_CTA_OR_OFFER": True, "F5_BANNED_OR_SPECIAL": False},
            "is_violation": True,
            "regime": "UNMARKED_AD_NO_ERID",
            "confidence": 0.95,
        },
    ]
    mq_disp = build_micro_question(
        {"post_id": 888002, "text_window": "По промокоду START20 скидка 20% в боте https://t.me/crm_helper_bot"},
        t0_disp,
        votes_disp,
    )
    rec_disp = {
        "post_id": 888002,
        "url": "https://vc.ru/marketing/888002-promo-case",
        "rubric": "marketing",
        "title": "Как мы ускорили отдел продаж с CRM-ботом",
        "text_window": "По промокоду START20 скидка 20% в боте https://t.me/crm_helper_bot",
        "full_text": "По промокоду START20 скидка 20% в боте https://t.me/crm_helper_bot",
        "external_links": ["https://t.me/crm_helper_bot"],
        "commercial_targets": ["https://t.me/crm_helper_bot"],
        "erids": [],
        "has_strict_ad_label": False,
        "is_promo": False,
        "tier0_passport": t0_disp,
        "ensemble": {
            "ensemble_status": "NEEDS_MICRO_Q",
            "consensus_regime": None,
            "votes": votes_disp,
            "micro_question": mq_disp,
        },
    }
    rec_human = {
        "post_id": 888003,
        "url": "https://vc.ru/services/888003-human-verified",
        "rubric": "services",
        "title": "Человеческая верификация спорной статьи",
        "text_window": "Статья с прямой продажей подписки на сервис аналитики.",
        "full_text": "Статья с прямой продажей подписки на сервис аналитики.",
        "external_links": ["https://analytics-pro.ru"],
        "commercial_targets": ["https://analytics-pro.ru"],
        "erids": [],
        "has_strict_ad_label": False,
        "is_promo": False,
        "human_label": "UNMARKED_AD_NO_ERID",
    }

    ens_path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in (rec_auto, rec_disp, rec_human)) + "\n",
        encoding="utf-8",
    )

    # 1. Export chat batch -> only disputed (888002) should be in batch_01.md
    batches_dir = tmp_path / "chat_batches"
    b_files = export_chat_batches(ens_path, batches_dir, batch_size=10, only_disputed=True)
    assert len(b_files) == 1
    md_txt = b_files[0].read_text(encoding="utf-8")
    assert "post_id=888002" in md_txt
    assert "post_id=888001" not in md_txt

    # 2. Generate Micro-Question Desk v2 HTML (zero external CDN)
    html_out = tmp_path / "labeler.html"
    generate_labeler_page(input_jsonl=ens_path, output_html=html_out)
    html_txt = html_out.read_text(encoding="utf-8")
    assert "Пульт Микро-Вопросов v2" in html_txt
    assert "fonts.googleapis.com" not in html_txt
    assert "cdn.jsdelivr" not in html_txt

    # 3. Import external chat JSON agreeing with 3/3 votes on 888002 -> promotes to AUTO_GOLD!
    chat_json_file = tmp_path / "chat_reply.json"
    chat_json_file.write_text(
        json.dumps(
            [
                {
                    "post_id": 888002,
                    "F1": True,
                    "F2": False,
                    "F3": False,
                    "F4": True,
                    "F5": False,
                    "regime": "UNMARKED_AD_NO_ERID",
                    "confidence": 0.96,
                    "doubt_question_ru": "",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    imp_res = import_chat_batch_json(ens_path, chat_json_file)
    assert imp_res["promoted_to_auto_gold"] == 1

    # 4. Export clean training JSONL -> 2 AUTO_GOLD (w=2.0) + 1 HUMAN_VERIFIED (w=3.0), 0 golden leakage
    golden_ids, _ = load_golden_exclusions()
    assert 3255318 in golden_ids
    labeled_out = tmp_path / "vcru_human_labeled.jsonl"
    exp_res = export_clean_training_jsonl(ens_path, labeled_out)
    assert exp_res["human_verified_w3"] == 1
    assert exp_res["auto_gold_w2"] == 2
    assert exp_res["exported_total"] == 3

    # 5. Verify train_link_classifier loader respects w=3 and w=2 and split isolation
    tr_w, cal_w, st = _load_safe_real_vcru_cases(str(labeled_out))
    assert st["human_verified_unique_w3"] == 1
    assert st["unanimous_consensus_unique_w2"] == 2
    assert (len(tr_w) + len(cal_w)) > 3


def test_tier1a_sheaf_live_bundle_integration(champion_bundle):
    rec = {
        "post_id": 777001,
        "url": "https://vc.ru/tech/777001-review",
        "title": "Обзор ноутбука с промокодом",
        "full_text": "По промокоду TECH15 действует скидка 15% в магазине https://shop-tech.ru/?utm_campaign=vc",
        "external_links": ["https://shop-tech.ru/?utm_campaign=vc"],
        "commercial_targets": ["https://shop-tech.ru/?utm_campaign=vc"],
    }
    t0 = build_tier0_passport(rec)
    vote = evaluate_tier1a_sheaf(champion_bundle, rec, t0)
    assert vote["voter"] == "LinkCentric-V3"
    assert vote["family"] == "link_centric_local"
    assert isinstance(vote["facts"], dict)
    assert set(vote["facts"].keys()) == {
        "F1_OBJECT_PROMOTED",
        "F2_AUTHOR_SELF_BIO",
        "F3_UNIFORM_MULTI_REVIEW",
        "F4_COMMERCIAL_CTA_OR_OFFER",
        "F5_BANNED_OR_SPECIAL",
    }
