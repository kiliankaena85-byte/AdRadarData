"""
Tests for Automated Reality Verifier (Stage A 6-Point Legal Proof Matrix + Stage B Adversarial Skeptic),
CLI `verify-leads`, and DualAuditor real-world blind-spot guards (>365d statute, image captions, own blog).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

from legal_sheaf.dual_auditor import (
    DualAuditor,
    _extract_vcru_block_captions_and_links,
    _timestamp_to_iso_date,
)
from legal_sheaf.inn_erid_validators import extract_erid_tokens, validate_erid_token_format
from legal_sheaf.factual_verifiers import verify_per_link_erid_coverage
from legal_sheaf.reality_verifier import (
    compute_final_consensus,
    evaluate_lead_proof_matrix,
    run_adversarial_skeptic_for_lead,
    verify_hunt_leads_reality,
)


def _recent_iso(days_ago: int = 10) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _sample_hard_lead(tmp_path: Path, days_ago: int = 10) -> Dict[str, Any]:
    draft_path = tmp_path / "draft.txt"
    draft_path.write_text("Добрый день! Нашли реферальную ссылку без erid и форму без Политики.", encoding="utf-8")
    return {
        "advertiser_domain": "alpha-crm.ru",
        "related_articles_count": 1,
        "ledger_status": "NEW",
        "lead_package": {
            "template_type": "AFFILIATE_SHIELD",
            "link_kind": "affiliate",
            "outreach_draft_path": str(draft_path),
        },
        "related_articles": [
            {
                "title": "Как Alpha CRM автоматизирует продажи",
                "url": "https://vc.ru/services/999001",
                "published_at": _recent_iso(days_ago),
                "scenario_38fz": "UNMARKED_AD_NO_ERID",
                "is_violation_38fz": True,
                "verdict_38fz": "Партнерская реф-ссылка без erid",
                "violation_probability": 0.91,
                "commercial_links": ["https://alpha-crm.ru/?ref=vcpartner"],
                "traffic_light": {
                    "RED_REAL_FINE": [
                        {
                            "signature": "unmarked_ad_no_erid",
                            "witness": {
                                "source_url": "https://vc.ru/services/999001",
                                "quote": "https://alpha-crm.ru/?ref=vcpartner",
                            },
                        }
                    ]
                },
            }
        ],
        "site_audit": {
            "status": "OK",
            "company_profile": {
                "inn": "7707083893",
                "legal_name": "ООО «Альфа Системс»",
                "is_foreign_kio": False,
                "entity_type": "LEGAL_ENTITY",
            },
            "rkn_registry": {"status": "FOUND"},
            "traffic_light": {
                "RED_REAL_FINE": [
                    {
                        "signature": "missing-privacy-policy-link",
                        "koap_article": "13.11_part_3",
                        "legal_basis": "ч. 3 ст. 13.11 КоАП РФ, ч. 2 ст. 18.1 152-ФЗ",
                        "description": "Форма обратной связи без ссылки на Политику ПД",
                        "witness": {
                            "source_url": "https://alpha-crm.ru/contacts",
                            "quote": "<form><input name='phone'></form>",
                        },
                    },
                    {
                        "signature": "google-analytics",
                        "koap_article": "13.11_part_8",
                        "legal_basis": "ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
                        "description": "Скрипт google-analytics.com",
                        "witness": {
                            "source_url": "https://alpha-crm.ru/",
                            "quote": "https://www.google-analytics.com/analytics.js",
                        },
                    },
                ],
                "ORANGE_REGULATORY_DEMAND": [],
            },
            "penalties": {
                "applicable_min_rubles": 1000000,
                "applicable_max_rubles": 6000000,
                "total_min_rubles": 1000000,
                "total_max_rubles": 6000000,
            },
        },
    }


def test_stage_a_confirmed_hard_fact_lead(tmp_path: Path):
    lead = _sample_hard_lead(tmp_path, days_ago=14)
    lead["site_audit"]["traffic_light"]["ORANGE_REGULATORY_DEMAND"] = [
        {
            "signature": "mintsifry_it_accreditation_risk",
            "koap_article": "mintsifry_511",
            "legal_basis": "Приказ Минцифры России № 511",
            "description": "Риск аккредитации ИТ-компании",
            "witness": {"source_url": "https://alpha-crm.ru/", "quote": "IT stack"},
        },
        {
            "signature": "commercial_b2c_foreign_words_168fz",
            "koap_article": "168_fz",
            "legal_basis": "Федеральный закон № 168-ФЗ",
            "description": "Иностранные слова в интерфейсе",
            "witness": {"source_url": "https://alpha-crm.ru/", "quote": "Pricing"},
        },
    ]
    pm = evaluate_lead_proof_matrix(lead)
    assert pm["stage_a_verdict"] == "CONFIRMED_HARD_FACT"
    assert all(v["status"] == "PASS" for v in pm["criteria"].values())
    assert len(pm["hard_facts"]) == 3
    assert len(pm["soft_findings"]) == 2
    assert pm["rejected_reasons"] == []


def test_stage_a_rejects_foreign_kio_and_connection_failed(tmp_path: Path):
    kio_lead = _sample_hard_lead(tmp_path)
    kio_lead["site_audit"]["company_profile"]["inn"] = "9909123456"
    kio_lead["site_audit"]["company_profile"]["is_foreign_kio"] = True
    pm_kio = evaluate_lead_proof_matrix(kio_lead)
    assert pm_kio["stage_a_verdict"] == "AUTO_REJECTED_FP"
    assert pm_kio["criteria"]["C1_entity_russian_jurisdiction"]["status"] == "FAIL"

    conn_lead = _sample_hard_lead(tmp_path)
    conn_lead["site_audit"]["status"] = "CONNECTION_FAILED"
    pm_conn = evaluate_lead_proof_matrix(conn_lead)
    assert pm_conn["stage_a_verdict"] == "AUTO_REJECTED_FP"


def test_stage_a_rejects_expired_article_and_broken_witness():
    expired_lead = {
        "advertiser_domain": "old-promo.ru",
        "lead_package": {"template_type": "AFFILIATE_SHIELD", "link_kind": "affiliate"},
        "related_articles": [
            {
                "title": "Старый пост 2023 года",
                "url": "https://vc.ru/marketing/111222",
                "published_at": "2023-05-10",
                "scenario_38fz": "UNMARKED_AD_NO_ERID",
                "is_violation_38fz": True,
                "commercial_links": ["https://old-promo.ru/?ref=old"],
            }
        ],
        "site_audit": {
            "status": "OK",
            "company_profile": {"inn": "7707083893", "legal_name": "ООО Старое"},
            "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": []},
            "penalties": {},
        },
    }
    pm_exp = evaluate_lead_proof_matrix(expired_lead)
    assert pm_exp["stage_a_verdict"] == "AUTO_REJECTED_FP"
    assert pm_exp["criteria"]["C3_temporal_validity"]["status"] == "FAIL"

    broken_witness_lead = {
        "advertiser_domain": "broken-witness.ru",
        "related_articles": [],
        "site_audit": {
            "status": "OK",
            "company_profile": {"inn": "7707083893", "legal_name": "ООО Брокен"},
            "traffic_light": {
                "RED_REAL_FINE": [
                    {
                        "signature": "missing-privacy-policy-link",
                        "koap_article": "13.11_part_3",
                        "witness": {"source_url": "", "quote": ""},
                    }
                ],
                "ORANGE_REGULATORY_DEMAND": [],
            },
            "penalties": {},
        },
    }
    pm_bw = evaluate_lead_proof_matrix(broken_witness_lead)
    assert pm_bw["stage_a_verdict"] == "AUTO_REJECTED_FP"
    assert pm_bw["criteria"]["C2_witness_integrity"]["status"] == "FAIL"


def test_stage_a_own_blog_and_needs_human_check():
    own_blog_lead = {
        "advertiser_domain": "myblog-crm.ru",
        "related_articles": [
            {
                "title": "Как мы обновили личный кабинет",
                "url": "https://vc.ru/myblogcrm/777888",
                "published_at": _recent_iso(5),
                "ownership_check": {"is_own_blog": True, "matched_domain": "myblog-crm.ru"},
                "scenario_38fz": "OWN_BLOG_INFORMING_GREY",
                "is_violation_38fz": False,
                "commercial_links": ["https://myblog-crm.ru/cabinet"],
            }
        ],
        "site_audit": {
            "status": "OK",
            "company_profile": {"inn": "7707083893", "legal_name": "ООО Блог"},
            "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": []},
            "penalties": {},
        },
    }
    pm_own = evaluate_lead_proof_matrix(own_blog_lead)
    assert pm_own["stage_a_verdict"] == "AUTO_REJECTED_FP"
    assert pm_own["criteria"]["C4_exclusion_immunity_check"]["status"] == "FAIL"

    missing_inn_lead = {
        "advertiser_domain": "anon-site.ru",
        "related_articles": [],
        "site_audit": {
            "status": "OK",
            "company_profile": {"inn": None, "legal_name": "Компания (anon-site.ru)"},
            "traffic_light": {
                "RED_REAL_FINE": [
                    {
                        "signature": "missing-privacy-policy-link",
                        "koap_article": "13.11_part_3",
                        "legal_basis": "ч. 3 ст. 13.11 КоАП РФ",
                        "witness": {"source_url": "https://anon-site.ru/", "quote": "<form></form>"},
                    }
                ],
                "ORANGE_REGULATORY_DEMAND": [],
            },
            "penalties": {},
        },
    }
    pm_warn = evaluate_lead_proof_matrix(missing_inn_lead)
    assert pm_warn["stage_a_verdict"] == "NEEDS_HUMAN_CHECK"
    assert pm_warn["criteria"]["C1_entity_russian_jurisdiction"]["status"] == "WARN"


def test_dual_auditor_statute_expired_and_own_blog_guards(champion_bundle):
    auditor = DualAuditor(bundle=champion_bundle)

    # 1. Expired article (>365 days) must NOT raise is_violation_38fz
    res_exp = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/marketing/555001",
            text="Лучшая облачная АТС со скидкой! Используйте промокод OLD2023 по ссылке https://test-ats.ru/?ref=vc23",
            raw_links=["https://test-ats.ru/?ref=vc23"],
            published_at_hint="2023-06-01",
            verify_links_live=False,
        )
    )
    assert res_exp["statute_expired_365d"] is True
    assert res_exp["is_violation_38fz"] is False
    assert res_exp["scenario_38fz"] == "STATUTE_EXPIRED_4_5_KOAP"
    assert "4.5" in res_exp["verdict_38fz"] or "1.7" in res_exp["verdict_38fz"]

    # 2. Own corporate blog WITHOUT promo code or strict ref -> OWN_BLOG_INFORMING_GREY (not RED fine)
    res_own_grey = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/testbrandofficial/555002",
            text=(
                "Мы обновили наш сервис TestBrandOfficial и добавили темную тему, быстрый экспорт и удобные тарифы. "
                "Подробности об обновлении нашей платформы на сайте https://testbrandofficial.ru/changelog?utm_source=vc"
            ),
            raw_links=["https://testbrandofficial.ru/changelog?utm_source=vc"],
            author_hint="TestBrandOfficial",
            subsite_hint="TestBrandOfficial",
            published_at_hint=_recent_iso(7),
            verify_links_live=False,
        )
    )
    assert res_own_grey["ownership_check"]["is_own_blog"] is True
    assert res_own_grey["scenario_38fz"] == "OWN_BLOG_INFORMING_GREY"
    assert res_own_grey["is_violation_38fz"] is False

    # 3. Own corporate blog WITH explicit promo code + partner ref -> stays UNMARKED_AD_NO_ERID
    res_own_promo = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/testbrandofficial/555003",
            text="Спецпредложение! Покупайте тариф прямо сейчас, промокод BRAND50 по партнерской ссылке https://testbrandofficial.ru/?ref=vcblog",
            raw_links=["https://testbrandofficial.ru/?ref=vcblog"],
            author_hint="TestBrandOfficial",
            subsite_hint="TestBrandOfficial",
            published_at_hint=_recent_iso(7),
            verify_links_live=False,
        )
    )
    assert res_own_promo["scenario_38fz"] == "UNMARKED_AD_NO_ERID"
    assert res_own_promo["is_violation_38fz"] is True


def test_vcru_caption_block_extraction_and_timestamp_helpers():
    blocks = [
        {"type": "text", "data": {"text": "<p>Обзор нового сервиса доставки.</p>"}},
        {
            "type": "media",
            "data": {
                "items": [
                    {
                        "title": "Баннер партнера",
                        "caption": "Реклама ООО Вектор ИНН 7707083893 erid: 2Vfnxwk99AA <a href='https://vector-deliv.ru'>Сайт</a>",
                    }
                ]
            },
        },
    ]
    texts, links, _ = _extract_vcru_block_captions_and_links(blocks)
    joined = " ".join(texts)
    assert "erid: 2Vfnxwk99AA" in joined
    assert "https://vector-deliv.ru" in links
    assert _timestamp_to_iso_date(1700000000) == "2023-11-14"


def test_stage_b_skeptic_2_family_arbitration_pii_and_citation_lock(tmp_path: Path):
    lead = _sample_hard_lead(tmp_path)
    # Inject PII to verify sanitizer strips it before outbound POST
    lead["related_articles"][0]["verdict_38fz"] = "Контакт клиент Иванов Иван Иванович, test.client@mail.ru, +7 999 123-45-67"
    pm = evaluate_lead_proof_matrix(lead)

    seen_gemini_urls: List[str] = []

    def _make_gemini_transport(verdicts_by_model: Dict[str, str], require_redacted_markers: bool = True):
        def handler(request: httpx.Request) -> httpx.Response:
            seen_gemini_urls.append(str(request.url))
            assert request.headers.get("x-goog-api-key") == "AIzaTestGeminiKey"
            body = json.loads(request.content.decode("utf-8"))
            assert body["generationConfig"]["responseMimeType"] == "application/json"
            user_msg = body["contents"][0]["parts"][0]["text"]
            assert "test.client@mail.ru" not in user_msg
            assert "999 123-45-67" not in user_msg
            if require_redacted_markers:
                assert "[EMAIL_REDACTED_152FZ]" in user_msg
                assert "[PHONE_REDACTED_152FZ]" in user_msg

            url_str = str(request.url)
            model_id = url_str.split("/models/")[-1].split(":")[0]
            v = verdicts_by_model.get(model_id, "UPHOLD_VIOLATION")
            payload = {
                "skeptic_verdict": v,
                "confidence": 0.88,
                "strongest_defense_argument_ru": (
                    "Форма и аналитика подтверждены (ч. 8 ст. 13.11 КоАП РФ), "
                    "ложные ссылки на А40-999999/2026 и ст. 99.9 КоАП РФ."
                ),
                "verified_hard_facts": ["google-analytics"],
                "challenged_findings": [],
                "citation_basis": ["ч. 16 ст. 14.3 КоАП РФ"],
            }
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "{\"ignore\": true}"},
                                    {"text": json.dumps(payload, ensure_ascii=False)},
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.MockTransport(handler)

    # Case 1 (Default provider='gemini'): Unanimous UPHOLD_VIOLATION -> verifies Gemini + Citation Lock
    async def _run_uphold() -> Dict[str, Any]:
        async with httpx.AsyncClient(
            transport=_make_gemini_transport({
                "gemini-3-flash-preview": "UPHOLD_VIOLATION",
                "gemini-2.5-flash": "UPHOLD_VIOLATION",
            })
        ) as client:
            return await run_adversarial_skeptic_for_lead(
                lead, pm, client=client, api_keys=["AIzaTestGeminiKey"]
            )

    res_uphold = asyncio.run(_run_uphold())
    assert res_uphold["status"] == "OK"
    assert res_uphold["responding_families_count"] == 2
    assert res_uphold["pii_redacted"] is True
    assert any("gemini-3-flash-preview" in u for u in seen_gemini_urls)
    assert any("gemini-2.5-flash" in u for u in seen_gemini_urls)
    for fam in res_uphold["families"]:
        arg = fam["strongest_defense_argument_ru"]
        assert "А40-999999/2026" not in arg
        assert "99.9 КоАП" not in arg
        assert "13.11 КоАП" in arg
        assert "[номер исключен политикой верификации]" in arg

    c_uphold = compute_final_consensus(pm, res_uphold)
    assert c_uphold["final_verdict"] == "AUTO_APPROVED_HARD_FACT"

    split_skeptic = {
        "status": "OK",
        "families": [
            {"family_id": "family_1_gemini_v3", "status": "OK", "skeptic_verdict": "UPHOLD_VIOLATION"},
            {"family_id": "family_2_gemini_v25", "status": "OK", "skeptic_verdict": "CHALLENGE_BORDERLINE"},
        ],
    }
    c_split = compute_final_consensus(pm, split_skeptic)
    assert c_split["final_verdict"] == "NEEDS_HUMAN_CHECK"

    # Case 2 (Default provider='gemini'): Unanimous DISMISS_FALSE_POSITIVE overrides Stage A via verify_hunt_leads_reality
    hunt_file = tmp_path / "hunt_vcru_consolidated_report.json"
    lead_xss = _sample_hard_lead(tmp_path)
    lead_xss["advertiser_domain"] = "xss-crm.ru"
    lead_xss["site_audit"]["company_profile"]["legal_name"] = "ООО <script>alert(1)</script>"
    hunt_file.write_text(
        json.dumps({"consolidated_company_reports": [lead, lead_xss]}, ensure_ascii=False),
        encoding="utf-8",
    )

    out_dir = tmp_path / "out_reports"

    async def _run_dismiss() -> Dict[str, Any]:
        async with httpx.AsyncClient(
            transport=_make_gemini_transport(
                {
                    "gemini-3-flash-preview": "DISMISS_FALSE_POSITIVE",
                    "gemini-2.5-flash": "DISMISS_FALSE_POSITIVE",
                },
                require_redacted_markers=False,
            )
        ) as dismiss_client:
            return await verify_hunt_leads_reality(
                report_path=hunt_file,
                with_skeptic=True,
                max_leads=5,
                output_dir=out_dir,
                skeptic_client=dismiss_client,
                api_keys=["AIzaTestGeminiKey"],
            )

    rep = asyncio.run(_run_dismiss())
    assert rep["provider"] == "gemini"
    assert rep["summary"]["total_leads_checked"] == 2
    assert rep["summary"]["auto_rejected_fp"] == 2
    assert (out_dir / "reality_verification_report.json").exists()
    html_text = (out_dir / "reality_verification_report.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "fonts.googleapis.com" not in html_text
    assert "<script src=" not in html_text
    assert Path(rep["feedback_jsonl_path"]).exists()

    # Case 3: Legacy OpenRouter fallback (provider='openrouter') + reasoning fallback
    def _reasoning_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "max_tokens" not in body
        payload = {
            "skeptic_verdict": "UPHOLD_VIOLATION",
            "confidence": 0.91,
            "strongest_defense_argument_ru": "Форма без политики по ч. 3 ст. 13.11 КоАП РФ.",
            "verified_hard_facts": ["missing-privacy-policy-link"],
            "challenged_findings": [],
            "citation_basis": ["ч. 3 ст. 13.11 КоАП РФ"],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning": f"Думаю по фактам: ```json\n{json.dumps(payload, ensure_ascii=False)}\n```",
                        }
                    }
                ]
            },
        )

    async def _run_reasoning() -> Dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_reasoning_handler)) as rc:
            return await run_adversarial_skeptic_for_lead(
                lead, pm, client=rc, api_keys=["sk-test-key-1"], provider="openrouter"
            )

    res_reasoning = asyncio.run(_run_reasoning())
    assert res_reasoning["status"] == "OK"
    assert res_reasoning["responding_families_count"] == 2


def test_long_70char_ord_erid_token_valid():
    long_erid = "ADLVwa2EeAfT1KcczwC8jV6DKRECFDC5vR4j72xE3AXzBxGwCV7a2JkhJ4PF9YpbuFjbPt8e"
    assert len(long_erid) > 64
    assert validate_erid_token_format(long_erid) is True

    url = f"https://affid.ru/g/787beb69ca7cf971b354a7LceXep6nCopikENsDwSu/?erid={long_erid}&subid1=vc"
    tokens = extract_erid_tokens("", page_url=url)
    assert long_erid in tokens

    cov = verify_per_link_erid_coverage(
        text="Реклама. ООО Партнер. ИНН 7707083893.",
        commercial_urls=[url],
        raw_links=[url],
        page_url="https://vc.ru/finance/999999",
    )
    assert cov["marked_links_count"] == 1
    assert cov["invalid_erid_links_count"] == 0
    assert cov["has_partial_erid_gap"] is False

