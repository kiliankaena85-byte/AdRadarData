"""
AdRadar Automated Reality Verifier (Stage A + Stage B).
Two-stage post-hunt verification pipeline:
- Stage A (Deterministic 6-Point Legal Proof Matrix C1..C6):
  Verifies Russian subject jurisdiction (INN checksum, non-KIO), Witness integrity,
  1-year KoAP Art. 4.5 statute of limitations, statutory exclusions (own blog / moratorium),
  closed KoAP/Citation whitelist compliance, and Outreach-to-Fact alignment.
  Splits every lead's issues into indisputable `hard_facts` vs context-dependent `soft_findings`.
- Stage B (Adversarial 2-Family OpenRouter Defense Attorney):
  Invokes two independent free LLM families loaded dynamically from `config/openrouter_models.yaml`
  in the persona of "Адвокат ответчика в ФАС и Роскомнадзоре" after 152-FZ PII redaction and
  post-filtered through Citation Lock.
- Produces unified consensus verdicts (`AUTO_APPROVED_HARD_FACT`, `NEEDS_HUMAN_CHECK`, `AUTO_REJECTED_FP`),
  JSON/HTML reports (strictly self-contained, zero external CDNs), and `auto_skeptic_feedback.jsonl`.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import yaml

from legal_sheaf.chronotope import TemporalValidityGate
from legal_sheaf.config import ADRADAR_DATA_DIR
from legal_sheaf.inn_erid_validators import (
    AFFILIATE_REGEX,
    PROMO_CODE_REGEX,
    validate_inn_checksum,
)
from legal_sheaf.schema import OffenseContinuity
from legal_sheaf.triad_decision_router import sanitize_before_external_decision

logger = logging.getLogger("legal_sheaf.reality_verifier")

OPENROUTER_CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

HARD_SITE_SIGNATURES: Set[str] = {
    "missing-privacy-policy-link",
    "missing-consent-checkbox",
    "prechecked-consent-checkbox",
    "bundled-offer-consent-missing-separate-consent",
    "google-analytics",
    "google-tag-manager",
    "google-forms",
    "foreign_widget_data_leak",
    "toxic_supply_chain_widget",
    "instant_protocol_trigger",
    "missing_seller_requisites",
    "illegal_no_refund_b2c_clause",
}

SOFT_OR_CONTEXTUAL_SIGNATURES: Set[str] = {
    "rkn_unregistered_operator",
    "cross_border_undeclared_in_rkn",
    "superlative_without_asterisk",
    "foreign_words_168fz_no_translation",
    "foreign-auth-google-apple",
    "it_accreditation_disclosure_missing",
    "regalia_chronology_mismatch",
    "extremist_meta_attribution_missing",
}

DEFAULT_FALLBACK_KOAP_ARTICLES: Set[str] = {
    "1.5", "1.7", "4.1.1", "4.1.2", "4.4", "4.5",
    "13.11", "13.15", "13.55", "13.56", "14.3", "14.7", "14.8", "20.3", "24.5", "26.2",
}

ALLOWED_NON_KOAP_REGULATORY_TAGS: Set[str] = {
    "mintsifry_511",
    "168_fz",
    "248_fz_art_49",
    "decree_1286",
    "hygiene",
    "business_hygiene",
}


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    return Path(ADRADAR_DATA_DIR).resolve()


def _resolve_repo_root() -> Optional[Path]:
    """Locates the repository root safely whether running from checkout or .venv site-packages."""
    candidates = [Path.cwd()]
    here = Path(__file__).resolve()
    for p in here.parents:
        candidates.append(p)
    for cand in candidates:
        if (cand / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml").exists():
            return cand
        if (cand / "config" / "verified_citations_whitelist.yaml").exists():
            return cand.parent
    return None


def _ensure_vlm_import_path() -> None:
    repo_root = _resolve_repo_root()
    if repo_root is not None and str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _load_openrouter_models_config() -> Dict[str, Any]:
    repo_root = _resolve_repo_root()
    candidates: List[Path] = [
        Path.cwd() / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
        Path.cwd() / "config" / "openrouter_models.yaml",
        Path(__file__).resolve().parents[2] / "config" / "openrouter_models.yaml",
    ]
    if repo_root is not None:
        candidates.insert(0, repo_root / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml")

    for path in candidates:
        if path.exists():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    return loaded
            except Exception as e:
                logger.warning(f"Failed to load openrouter_models.yaml from {path}: {e}")
    return {}


def _get_openrouter_keys() -> List[str]:
    _ensure_vlm_import_path()
    try:
        from vlm_context_engine.openrouter_gateway import get_openrouter_api_keys
        keys = get_openrouter_api_keys()
        if keys:
            return keys
    except Exception:
        pass

    merged: List[str] = []
    for var_name in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEYS"):
        raw = os.environ.get(var_name, "")
        for part in re.split(r"[\s,;]+", str(raw or "").strip()):
            clean = part.strip().strip('"').strip("'")
            if clean and clean not in merged:
                merged.append(clean)
    if merged:
        return merged

    repo_root = _resolve_repo_root()
    env_candidates = [_active_data_dir() / ".env", Path.cwd() / ".env"]
    if repo_root is not None:
        env_candidates.append(repo_root / ".env")
    for env_path in env_candidates:
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEYS"):
                    for part in re.split(r"[\s,;]+", v.strip().strip('"').strip("'")):
                        if part and part not in merged:
                            merged.append(part)
        except Exception:
            continue
        if merged:
            return merged
    return []


def _load_gemini_models_config() -> Dict[str, Any]:
    repo_root = _resolve_repo_root()
    candidates: List[Path] = [
        Path.cwd() / "legal_sheaf_verifier" / "config" / "gemini_models.yaml",
        Path.cwd() / "config" / "gemini_models.yaml",
        Path(__file__).resolve().parents[2] / "config" / "gemini_models.yaml",
    ]
    if repo_root is not None:
        candidates.insert(0, repo_root / "legal_sheaf_verifier" / "config" / "gemini_models.yaml")

    for path in candidates:
        if path.exists():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    return loaded
            except Exception as e:
                logger.warning(f"Failed to load gemini_models.yaml from {path}: {e}")
    return {}


def _get_gemini_api_keys() -> List[str]:
    _ensure_vlm_import_path()
    try:
        from vlm_context_engine.gemini_gateway import get_gemini_api_keys
        keys = get_gemini_api_keys()
        if keys:
            return keys
    except Exception:
        pass

    merged: List[str] = []
    for var_name in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY"):
        raw = os.environ.get(var_name, "")
        for part in re.split(r"[\s,;]+", str(raw or "").strip()):
            clean = part.strip().strip('"').strip("'")
            if clean and clean not in merged:
                merged.append(clean)
    if merged:
        return merged

    repo_root = _resolve_repo_root()
    env_candidates = [_active_data_dir() / ".env", Path.cwd() / ".env"]
    if repo_root is not None:
        env_candidates.append(repo_root / ".env")
    for env_path in env_candidates:
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY"):
                    for part in re.split(r"[\s,;]+", v.strip().strip('"').strip("'")):
                        if part and part not in merged:
                            merged.append(part)
        except Exception:
            continue
        if merged:
            return merged
    return []


def _extract_gemini_text(payload: Dict[str, Any]) -> str:
    """Extracts non-thought text parts from Google Gemini generateContent response."""
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    regular: List[str] = []
    fallback: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        txt = part.get("text")
        if not isinstance(txt, str) or not txt.strip():
            continue
        fallback.append(txt)
        if not part.get("thought"):
            regular.append(txt)
    if regular:
        return "\n".join(regular).strip()
    return "\n".join(fallback).strip()


def _load_whitelist() -> Tuple[Set[str], Set[str]]:
    _ensure_vlm_import_path()
    try:
        from vlm_context_engine.openrouter_gateway import load_citation_whitelist
        cits, koaps = load_citation_whitelist()
        if cits or koaps:
            return set(cits), set(koaps)
    except Exception:
        pass

    repo_root = _resolve_repo_root()
    candidates = [
        Path.cwd() / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml",
        Path(__file__).resolve().parents[2] / "config" / "verified_citations_whitelist.yaml",
    ]
    if repo_root is not None:
        candidates.insert(0, repo_root / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml")
    for p in candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                cases = set(data.get("verified_court_cases", []))
                letters = set(data.get("verified_agency_letters", []))
                koap_direct = data.get("verified_koap_articles", []) or []
                koap_nested = (data.get("verified_koap_and_laws") or {}).get("koap_articles", []) or []
                allowed_koap = {str(x) for x in list(koap_direct) + list(koap_nested)}
                return cases | letters, (allowed_koap or set(DEFAULT_FALLBACK_KOAP_ARTICLES))
            except Exception:
                pass
    return set(), set(DEFAULT_FALLBACK_KOAP_ARTICLES)


def _sanitize_text_safe(text: str, allowed_citations: Set[str], allowed_koap: Set[str]) -> str:
    if not text:
        return ""
    _ensure_vlm_import_path()
    try:
        from vlm_context_engine.openrouter_gateway import sanitize_model_citations
        return str(sanitize_model_citations(text, allowed_citations, allowed_koap))
    except Exception:
        cleaned = str(text)
        for pat in (
            re.compile(r"\b0?5-\d{3,5}/\d{2,4}/20\d{2}\b"),
            re.compile(r"\b[АA]\d{2}-\d{3,7}/20\d{2}\b"),
            re.compile(r"\b\d{2}АП-\d{3,7}/20\d{2}\b"),
            re.compile(r"\b(?:[А-ЯЁ]{2}|\d{2})/\d{4,6}/\d{2}\b"),
        ):
            cleaned = pat.sub(
                lambda m: m.group(0) if m.group(0) in allowed_citations else "[номер исключен политикой верификации]",
                cleaned,
            )
        if allowed_koap:
            pat_koap = re.compile(
                r"(?i)\b(?:ч\.\s*[\d,/-]+\s*)?(?:ст\.|стать[еяию]\w*)\s*(\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*КоАП"
            )
            cleaned = pat_koap.sub(
                lambda m: m.group(0) if m.group(1) in allowed_koap else "[статья КоАП исключена политикой верификации]",
                cleaned,
            )
        return cleaned


def _extract_json_safe(raw_response: str) -> Dict[str, Any]:
    _ensure_vlm_import_path()
    try:
        from vlm_context_engine.openrouter_gateway import extract_json_resilient
        return extract_json_resilient(raw_response)
    except Exception:
        cleaned = re.sub(r"(?is)<think>.*?</think>", "", str(raw_response or "")).strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if m:
                cleaned = m.group(1).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"No JSON object in skeptic response: {cleaned[:120]}")
        parsed = json.loads(cleaned[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("Skeptic response JSON is not an object")
        return parsed


def _witness_is_intact(finding: Dict[str, Any]) -> bool:
    w = finding.get("witness")
    if not isinstance(w, dict):
        return False
    src = str(w.get("source_url") or "").strip()
    quote = str(w.get("quote") or "").strip()
    raw_snip = str(w.get("raw_snippet") or "").strip()
    rkn_status = str(w.get("rkn_response_status") or "").strip()
    if not src.startswith(("http://", "https://")):
        return False
    return bool(quote or raw_snip or rkn_status)


def _extract_koap_base(koap_str: str) -> Optional[str]:
    m = re.search(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d+)?)(?![\d.])", str(koap_str or ""))
    return m.group(1) if m else None


def _is_article_expired(article: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if article.get("statute_expired_365d"):
        return True, str(article.get("verdict_38fz") or "Истек 1-летний срок давности по ст. 4.5 КоАП РФ")
    pub_at = article.get("published_at")
    if not pub_at:
        return False, None
    td = TemporalValidityGate().evaluate(
        published_at=str(pub_at),
        offense_type="ad_marking",
        continuity=OffenseContinuity.COMPLETED,
    )
    if not td.is_punishable:
        return True, td.rationale
    return False, td.rationale


def _has_hard_article_evidence(article: Dict[str, Any]) -> bool:
    scen = str(article.get("scenario_38fz") or "")
    if scen in ("INCOMPLETE_MARKING_LABEL_OR_INN", "PARTIAL_ERID_MARKING_GAP"):
        return True
    if scen == "BANNED_OR_SPECIAL_REGIME":
        tl = article.get("traffic_light") or {}
        reds = tl.get("RED_REAL_FINE") or []
        return bool(reds and any(_witness_is_intact(f) for f in reds))
    if scen != "UNMARKED_AD_NO_ERID":
        return False

    links = [str(u) for u in (article.get("commercial_links") or []) if u]
    verdict_txt = str(article.get("verdict_38fz") or "")
    title_txt = str(article.get("title") or "")
    combined = f"{title_txt} {verdict_txt}"

    has_strict_aff = any(AFFILIATE_REGEX.search(u) for u in links) or bool(AFFILIATE_REGEX.search(combined))
    has_promo = bool(PROMO_CODE_REGEX.search(combined))
    if has_strict_aff or has_promo:
        return True

    per_link = article.get("per_link_erid_audit") or {}
    if per_link.get("has_partial_erid_gap"):
        return True

    for lv in article.get("link_landing_verifications") or []:
        if not isinstance(lv, dict):
            continue
        if lv.get("hidden_affiliate_network_detected") and not lv.get("hidden_erid_revealed"):
            return True
        if lv.get("is_confirmed_commercial_landing") and float(article.get("violation_probability") or 0.0) >= 0.70:
            return True

    return float(article.get("violation_probability") or 0.0) >= 0.78 and bool(links)


# =============================================================================
# STAGE A: DETERMINISTIC 6-POINT LEGAL PROOF MATRIX
# =============================================================================
def evaluate_lead_proof_matrix(company_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically audits a single consolidated advertiser report across 6 legal proof criteria (C1..C6)
    and splits issues into hard indisputable facts vs soft/borderline findings.
    """
    _, allowed_koap = _load_whitelist()
    domain = str(company_report.get("advertiser_domain") or "").strip()
    site_audit = company_report.get("site_audit") or {}
    cp = site_audit.get("company_profile") or {}
    inn = str(cp.get("inn") or "").strip()
    legal_name = str(cp.get("legal_name") or "").strip()
    is_foreign_kio = bool(cp.get("is_foreign_kio")) or inn.startswith("9909")
    site_status = str(site_audit.get("status") or "OK")
    rkn = site_audit.get("rkn_registry") or {}

    tl = site_audit.get("traffic_light") or {}
    reds = list(tl.get("RED_REAL_FINE") or [])
    oranges = list(tl.get("ORANGE_REGULATORY_DEMAND") or [])
    related_articles = list(company_report.get("related_articles") or [])
    lead_pkg = company_report.get("lead_package") or {}
    penalties = site_audit.get("penalties") or {}

    hard_facts: List[Dict[str, Any]] = []
    soft_findings: List[Dict[str, Any]] = []
    rejected_reasons: List[str] = []

    # --- C1: Russian Subject Jurisdiction ---
    inn_valid = bool(inn and not is_foreign_kio and validate_inn_checksum(inn))
    if is_foreign_kio:
        c1_status = "FAIL"
        c1_note = f"Иностранная организация / КИО (ИНН {inn}) не подпадает под прямую подсудность РФ по стандарту ICP."
        rejected_reasons.append(c1_note)
    elif site_status in ("CONNECTION_FAILED", "NON_HTML_RESPONSE"):
        c1_status = "FAIL"
        c1_note = f"Целевой сайт {domain} недоступен или вернул {site_status}."
        rejected_reasons.append(c1_note)
    elif inn_valid:
        c1_status = "PASS"
        c1_note = f"Подтвержден резидент РФ: {legal_name or domain}, валидный ИНН {inn}."
    else:
        c1_status = "WARN"
        c1_note = f"Домен {domain}: валидный ИНН на сайте не опубликован — требуется подтверждение юрлица до отправки КП."

    # --- Collect and Classify Site Findings + Witness Integrity (C2) ---
    broken_hard_witnesses: List[str] = []
    for f in reds:
        if not isinstance(f, dict):
            continue
        sig = str(f.get("signature") or "unknown")
        has_w = _witness_is_intact(f)
        koap = str(f.get("koap_article") or "")
        basis = str(f.get("legal_basis") or "")
        w = f.get("witness") or {}
        quote = str(w.get("quote") or w.get("raw_snippet") or "")[:200] if isinstance(w, dict) else ""
        src_url = str(w.get("source_url") or "") if isinstance(w, dict) else ""

        item = {
            "source": "SITE",
            "signature": sig,
            "koap_article": koap,
            "legal_basis": basis,
            "description": str(f.get("description") or sig),
            "source_url": src_url,
            "quote": quote,
            "witness_intact": has_w,
        }

        if sig == "foreign-auth-google-apple":
            soft_findings.append({**item, "soft_reason": "Действует мораторий по ст. 13.55 КоАП РФ"})
            continue

        if sig == "rkn_unregistered_operator":
            rkn_ok = str(rkn.get("status") or "").upper() == "NOT_FOUND" and not rkn.get("error_details")
            if has_w and rkn_ok:
                hard_facts.append(item)
            else:
                soft_findings.append({**item, "soft_reason": "Требуется ручная перепроверка реестра РКН (возможна регистрация по ОГРН/наименованию или сбой ответа)"})
            continue

        if sig in HARD_SITE_SIGNATURES:
            if has_w:
                hard_facts.append(item)
            else:
                broken_hard_witnesses.append(sig)
                soft_findings.append({**item, "soft_reason": "Отсутствует проверяемый Witness (URL или цитата DOM)"})
        else:
            soft_findings.append({**item, "soft_reason": "Контекстуальное нарушение (зависит от навигации/контекста)"})

    for f in oranges:
        if not isinstance(f, dict):
            continue
        sig = str(f.get("signature") or "orange_risk")
        w = f.get("witness") or {}
        soft_findings.append({
            "source": "SITE_ORANGE",
            "signature": sig,
            "koap_article": str(f.get("koap_article") or ""),
            "legal_basis": str(f.get("legal_basis") or ""),
            "description": str(f.get("description") or sig),
            "source_url": str(w.get("source_url") or "") if isinstance(w, dict) else "",
            "quote": str(w.get("quote") or w.get("raw_snippet") or "")[:180] if isinstance(w, dict) else "",
            "witness_intact": _witness_is_intact(f),
            "soft_reason": "Регуляторное требование / предупреждение по 248-ФЗ",
        })

    # --- Inspect Related VC.ru Articles (38-FZ, C3 Temporal, C4 Own-Blog Exclusion) ---
    flagged_articles: List[Dict[str, Any]] = [
        a for a in related_articles
        if isinstance(a, dict) and (a.get("is_violation_38fz") or str(a.get("scenario_38fz") or "") in ("STATUTE_EXPIRED_4_5_KOAP", "OWN_BLOG_INFORMING_GREY"))
    ]
    expired_articles_count = 0
    own_blog_articles_count = 0
    unknown_date_articles = 0
    active_violation_articles: List[Dict[str, Any]] = []

    for art in related_articles:
        if not isinstance(art, dict):
            continue
        scen = str(art.get("scenario_38fz") or "")
        is_viol = bool(art.get("is_violation_38fz"))
        expired, exp_note = _is_article_expired(art)
        ownership = art.get("ownership_check") or {}
        is_own_grey = scen == "OWN_BLOG_INFORMING_GREY" or (
            bool(ownership.get("is_own_blog"))
            and is_viol
            and scen in ("UNMARKED_AD_NO_ERID", "PARTIAL_ERID_MARKING_GAP")
            and not _has_hard_article_evidence(art)
        )

        if scen == "STATUTE_EXPIRED_4_5_KOAP" or (is_viol and expired):
            expired_articles_count += 1
            soft_findings.append({
                "source": "ARTICLE_EXPIRED",
                "signature": "statute_expired_4_5_koap",
                "koap_article": "4.5",
                "legal_basis": "ст. 4.5 КоАП РФ, ПП РФ № 1427",
                "description": f"Публикация устарела ({art.get('published_at')}): {exp_note or 'срок давности 1 год истек'}",
                "source_url": str(art.get("url") or art.get("article_url") or ""),
                "quote": str(art.get("title") or "")[:160],
                "witness_intact": False,
                "soft_reason": "Истек годичный срок давности по ст. 4.5 КоАП РФ",
            })
            continue

        if is_own_grey:
            own_blog_articles_count += 1
            soft_findings.append({
                "source": "ARTICLE_OWN_BLOG",
                "signature": "own_blog_informing_grey",
                "koap_article": "14.3_part_16",
                "legal_basis": "п. 2-3 ч. 2 ст. 2 38-ФЗ",
                "description": f"Собственный корпоративный блог ({ownership.get('matched_domain') or domain}): информирование о своем продукте",
                "source_url": str(art.get("url") or art.get("article_url") or ""),
                "quote": str(art.get("title") or "")[:160],
                "witness_intact": True,
                "soft_reason": "П. 2-3 ч. 2 ст. 2 38-ФЗ: собственный канал автора без явных промокодов/партнерских хвостов",
            })
            continue

        if not is_viol:
            continue

        active_violation_articles.append(art)
        if not art.get("published_at"):
            unknown_date_articles += 1

        art_url = str(art.get("url") or art.get("article_url") or "")
        art_tl = (art.get("traffic_light") or {}).get("RED_REAL_FINE") or []
        art_witness_intact = bool(
            (art_tl and any(_witness_is_intact(x) for x in art_tl))
            or (art_url.startswith(("http://", "https://")) and (art.get("commercial_links") or art.get("per_link_erid_audit")))
        )
        koap_art = "14.3_part_1" if scen == "BANNED_OR_SPECIAL_REGIME" else "14.3_part_16"
        art_item = {
            "source": "ARTICLE_38FZ",
            "signature": scen.lower() or "unmarked_ad_no_erid",
            "koap_article": koap_art,
            "legal_basis": "ч. 16 ст. 14.3 КоАП РФ, ст. 18.1 38-ФЗ" if koap_art == "14.3_part_16" else "ст. 7, 24, 28 38-ФЗ, ч. 1 ст. 14.3 КоАП РФ",
            "description": str(art.get("verdict_38fz") or f"Публикация {scen}: {art.get('title') or ''}"),
            "source_url": art_url,
            "quote": "; ".join(str(u) for u in (art.get("commercial_links") or [])[:2])[:180] or str(art.get("title") or "")[:180],
            "witness_intact": art_witness_intact,
            "published_at": art.get("published_at"),
        }

        if _has_hard_article_evidence(art) and art_witness_intact:
            hard_facts.append(art_item)
        else:
            if not art_witness_intact:
                broken_hard_witnesses.append(f"article:{scen}")
            soft_findings.append({**art_item, "soft_reason": "Отсутствует однозначный маркер (ref/промокод/подтверждение лендинга) или Witness"})

    # --- C2: Witness Integrity ---
    if broken_hard_witnesses and not hard_facts:
        c2_status = "FAIL"
        c2_note = f"Нарушена целостность доказательств (Witness): {', '.join(broken_hard_witnesses)}."
        rejected_reasons.append(c2_note)
    elif hard_facts and not broken_hard_witnesses:
        c2_status = "PASS"
        c2_note = f"Все жесткие факты ({len(hard_facts)}) подкреплены проверяемыми Witness (URL + DOM-цитата)."
    elif soft_findings:
        c2_status = "WARN"
        c2_note = f"Жестких фактов с полным Witness: {len(hard_facts)}, контекстуальных/мягких находок: {len(soft_findings)}."
    else:
        c2_status = "FAIL"
        c2_note = "Не найдено ни одного подтвержденного нарушения."
        rejected_reasons.append(c2_note)

    # --- C3: Temporal Statute-of-Limitations (Art. 4.5 KoAP RF) ---
    site_hard_facts = [h for h in hard_facts if h["source"] == "SITE"]
    if expired_articles_count > 0 and not active_violation_articles and not site_hard_facts and not soft_findings:
        c3_status = "FAIL"
        c3_note = "Все выявленные рекламные публикации вышли более 365 дней назад (ст. 4.5 КоАП РФ / ПП РФ № 1427)."
        rejected_reasons.append(c3_note)
    elif expired_articles_count > 0 and not active_violation_articles and not hard_facts:
        c3_status = "FAIL"
        c3_note = "Единственные триггеры по статьям погашены сроком давности 365 дней (ст. 4.5 КоАП РФ)."
        rejected_reasons.append(c3_note)
    elif unknown_date_articles > 0 and not site_hard_facts:
        c3_status = "WARN"
        c3_note = "Дата публикации статьи не определена в метаданных — перед предъявлением претензии по 38-ФЗ проверьте дату поста."
    else:
        c3_status = "PASS"
        c3_note = "Сроки давности (ст. 4.5 КоАП РФ) соблюдены (живой сайт = длящееся, статьи <= 365 дн.)."

    # --- C4: Exclusion & Immunity Check ---
    non_immune_findings = [
        x for x in (hard_facts + soft_findings)
        if x["signature"] not in ("own_blog_informing_grey", "statute_expired_4_5_koap", "foreign-auth-google-apple")
    ]
    if not non_immune_findings and (own_blog_articles_count > 0 or flagged_articles or oranges or reds):
        c4_status = "FAIL"
        c4_note = "Все выявленные признаки подпадают под законные исключения (п. 2-3 ч. 2 ст. 2 38-ФЗ, мораторий 13.55 или давность 4.5)."
        rejected_reasons.append(c4_note)
    elif not hard_facts and own_blog_articles_count > 0:
        c4_status = "WARN"
        c4_note = "В выборке присутствует собственный корпоративный блог (риск ложного срабатывания без явного промокода/ref)."
    else:
        c4_status = "PASS"
        c4_note = "Законных иммунитетов (ст. 2 38-ФЗ, мораторий) для активных жестких фактов не установлено."

    # --- C5: Closed KoAP & Whitelist Alignment ---
    bad_koap: List[str] = []
    for item in hard_facts + soft_findings:
        if item["signature"] in ("statute_expired_4_5_koap",):
            continue
        raw_koap = str(item.get("koap_article") or "").strip()
        if raw_koap.lower() in ALLOWED_NON_KOAP_REGULATORY_TAGS:
            continue
        if not raw_koap and item.get("source") == "SITE_ORANGE":
            continue
        base_art = _extract_koap_base(raw_koap)
        if not base_art or base_art not in allowed_koap:
            bad_koap.append(f"{item['signature']}:{raw_koap}")
    if bad_koap:
        c5_status = "FAIL"
        c5_note = f"Статья КоАП вне закрытого whitelist: {', '.join(bad_koap)}"
        rejected_reasons.append(c5_note)
    else:
        c5_status = "PASS"
        c5_note = "Все квалификации КоАП РФ и ФЗ строго соответствуют закрытому реестру verified_citations_whitelist.yaml."

    # --- C6: Outreach Claim Alignment ---
    c6_status = "PASS"
    c6_note = "Соответствие КП фактам аудита подтверждено."
    tpl = str(lead_pkg.get("template_type") or "").strip()
    l_kind = str(lead_pkg.get("link_kind") or "").strip()
    draft_path_str = str(lead_pkg.get("outreach_draft_path") or "").strip()

    if tpl in ("AFFILIATE_SHIELD", "DIRECT_POST_OR_OVERMARKED") and not active_violation_articles:
        c6_status = "FAIL"
        c6_note = (
            f"Рассогласование КП ({tpl}): в письме заявлено нарушение 38-ФЗ по статье, "
            "но после перепроверки активных нарушений в статьях не осталось (просрочено или собственный блог)."
        )
        rejected_reasons.append(c6_note)
    elif draft_path_str and Path(draft_path_str).exists():
        try:
            draft_txt = Path(draft_path_str).read_text(encoding="utf-8", errors="replace")
            if l_kind == "utm" and "реферальн" in draft_txt.lower():
                c6_status = "WARN"
                c6_note = "КП для UTM-ссылки содержит упоминание 'реферальн...' — требуется ручная вычитка формулировки."
            elif tpl == "PRAISE_POST_FIX_SITE" and not hard_facts and not soft_findings:
                c6_status = "WARN"
                c6_note = "КП PRAISE_POST_FIX_SITE сформировано без активных нарушений на сайте."
            else:
                c6_note = f"Текст КП ({tpl}/{l_kind or 'n/a'}) согласован с матрицей фактов."
        except Exception as e:
            c6_status = "WARN"
            c6_note = f"Не удалось прочитать файл КП {draft_path_str}: {e}"
    elif not lead_pkg:
        c6_note = "КП не упаковывалось (проверка матрицы пре-аутрича)."

    criteria = {
        "C1_entity_russian_jurisdiction": {"status": c1_status, "note": c1_note},
        "C2_witness_integrity": {"status": c2_status, "note": c2_note},
        "C3_temporal_validity": {"status": c3_status, "note": c3_note},
        "C4_exclusion_immunity_check": {"status": c4_status, "note": c4_note},
        "C5_koap_whitelist_alignment": {"status": c5_status, "note": c5_note},
        "C6_outreach_claim_alignment": {"status": c6_status, "note": c6_note},
    }

    if rejected_reasons:
        stage_a_verdict = "AUTO_REJECTED_FP"
    elif (
        len(hard_facts) >= 1
        and all(criteria[k]["status"] == "PASS" for k in criteria)
    ):
        stage_a_verdict = "CONFIRMED_HARD_FACT"
    else:
        stage_a_verdict = "NEEDS_HUMAN_CHECK"

    return {
        "domain": domain,
        "inn": inn or None,
        "legal_name": legal_name or None,
        "stage_a_verdict": stage_a_verdict,
        "criteria": criteria,
        "hard_facts": hard_facts,
        "soft_findings": soft_findings,
        "rejected_reasons": rejected_reasons,
        "penalty_range_rub": {
            "applicable_min": int(penalties.get("applicable_min_rubles") or 0),
            "applicable_max": int(penalties.get("applicable_max_rubles") or 0),
            "total_min": int(penalties.get("total_min_rubles") or 0),
            "total_max": int(penalties.get("total_max_rubles") or 0),
        },
        "summary_counts": {
            "hard_facts": len(hard_facts),
            "soft_findings": len(soft_findings),
            "expired_articles": expired_articles_count,
            "own_blog_articles": own_blog_articles_count,
        },
    }


# =============================================================================
# STAGE B: ADVERSARIAL 2-FAMILY OPENROUTER DEFENSE ATTORNEY
# =============================================================================
def _get_skeptic_model_families(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cfg = cfg or _load_openrouter_models_config()
    ej = cfg.get("ensemble_jury") or {}

    families: List[Dict[str, Any]] = []
    fam_nvidia = ej.get("tier_2b_nvidia") or {}
    nvidia_models = [
        m.strip() for m in [fam_nvidia.get("primary"), fam_nvidia.get("fallback")]
        if isinstance(m, str) and m.strip()
    ]
    if nvidia_models:
        families.append({"family_id": "nvidia_nemotron", "models": nvidia_models})

    fam_indep = ej.get("tier_2c_independent") or {}
    indep_models = [
        m.strip() for m in [
            fam_indep.get("primary"),
            fam_indep.get("fallback"),
            fam_indep.get("secondary_fallback"),
        ]
        if isinstance(m, str) and m.strip()
    ]
    if indep_models:
        families.append({"family_id": "independent_gemma_ernie", "models": indep_models})

    return families


def _build_anonymized_skeptic_payload(
    company_report: Dict[str, Any],
    proof_matrix: Dict[str, Any],
) -> Tuple[str, bool]:
    site_audit = company_report.get("site_audit") or {}
    cp = site_audit.get("company_profile") or {}
    lead_pkg = company_report.get("lead_package") or {}

    articles_brief: List[Dict[str, Any]] = []
    for a in (company_report.get("related_articles") or [])[:4]:
        if not isinstance(a, dict):
            continue
        articles_brief.append({
            "title": str(a.get("title") or "")[:160],
            "url": str(a.get("url") or a.get("article_url") or "")[:160],
            "published_at": a.get("published_at"),
            "scenario_38fz": a.get("scenario_38fz"),
            "is_violation_38fz": bool(a.get("is_violation_38fz")),
            "verdict_38fz": str(a.get("verdict_38fz") or "")[:240],
            "violation_probability": a.get("violation_probability"),
            "commercial_links": [str(u)[:140] for u in (a.get("commercial_links") or [])[:5]],
            "per_link_erid_audit": {
                k: (a.get("per_link_erid_audit") or {}).get(k)
                for k in ("total_commercial_links", "marked_links_count", "unmarked_links_count", "has_partial_erid_gap")
                if a.get("per_link_erid_audit")
            },
            "ownership_check": a.get("ownership_check"),
        })

    payload_obj = {
        "advertiser_domain": company_report.get("advertiser_domain"),
        "entity_type": cp.get("entity_type"),
        "rkn_status": (site_audit.get("rkn_registry") or {}).get("status"),
        "stage_a_verdict": proof_matrix.get("stage_a_verdict"),
        "criteria": proof_matrix.get("criteria"),
        "hard_facts": proof_matrix.get("hard_facts"),
        "soft_findings": proof_matrix.get("soft_findings"),
        "articles": articles_brief,
        "outreach_template_type": lead_pkg.get("template_type"),
        "outreach_link_kind": lead_pkg.get("link_kind"),
    }
    raw_json = json.dumps(payload_obj, ensure_ascii=False, indent=2)
    return sanitize_before_external_decision(raw_json)


def _normalize_skeptic_verdict(raw_val: Any) -> str:
    val = str(raw_val or "").strip().upper()
    if val in ("UPHOLD_VIOLATION", "CHALLENGE_BORDERLINE", "DISMISS_FALSE_POSITIVE"):
        return val
    if "DISMISS" in val or "FALSE" in val or "NO_VIOLATION" in val:
        return "DISMISS_FALSE_POSITIVE"
    if "UPHOLD" in val or "CONFIRM" in val:
        return "UPHOLD_VIOLATION"
    return "CHALLENGE_BORDERLINE"


def _get_gemini_skeptic_families(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cfg = cfg or _load_gemini_models_config()
    sj = cfg.get("reality_skeptic_jury") or {}
    banned = {str(b).strip().removeprefix("models/") for b in (cfg.get("banned_models") or [])}

    families: List[Dict[str, Any]] = []
    for fam_key in ("family_1_gemini_v3", "family_2_gemini_v25"):
        block = sj.get(fam_key) or {}
        raw_models = [block.get("primary")] + list(block.get("fallbacks") or [])
        models: List[str] = []
        for rm in raw_models:
            if not isinstance(rm, str):
                continue
            clean_m = rm.strip().removeprefix("models/")
            if clean_m and clean_m not in banned and clean_m not in models:
                models.append(clean_m)
        if models:
            families.append({"family_id": fam_key, "models": models})
    return families


def _parse_skeptic_json_and_build_result(
    fam_id: str,
    model_id: str,
    raw_content: str,
    allowed_citations: Set[str],
    allowed_koap: Set[str],
) -> Dict[str, Any]:
    parsed = _extract_json_safe(raw_content)
    verdict = _normalize_skeptic_verdict(parsed.get("skeptic_verdict"))
    try:
        conf = float(parsed.get("confidence", 0.5))
    except Exception:
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    arg_raw = str(parsed.get("strongest_defense_argument_ru") or "").strip()
    arg_safe = _sanitize_text_safe(arg_raw, allowed_citations, allowed_koap)
    hard_list = [
        _sanitize_text_safe(str(x), allowed_citations, allowed_koap)
        for x in (parsed.get("verified_hard_facts") or [])
    ]
    challenged_list = [
        _sanitize_text_safe(str(x), allowed_citations, allowed_koap)
        for x in (parsed.get("challenged_findings") or [])
    ]
    cits_list = [
        _sanitize_text_safe(str(x), allowed_citations, allowed_koap)
        for x in (parsed.get("citation_basis") or [])
    ]

    return {
        "family_id": fam_id,
        "status": "OK",
        "model_used": model_id,
        "skeptic_verdict": verdict,
        "confidence": round(conf, 3),
        "strongest_defense_argument_ru": arg_safe,
        "verified_hard_facts": hard_list,
        "challenged_findings": challenged_list,
        "citation_basis": cits_list,
    }


async def _invoke_single_gemini_skeptic_family(
    family: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    api_keys: List[str],
    allowed_citations: Set[str],
    allowed_koap: Set[str],
    client: httpx.AsyncClient,
    key_offset: int = 0,
) -> Dict[str, Any]:
    fam_id = family["family_id"]
    models = family["models"]
    last_error = "no_models_or_keys"

    for model_id in models:
        rotated_keys = api_keys[key_offset % len(api_keys) :] + api_keys[: key_offset % len(api_keys)]
        gen_config: Dict[str, Any] = {"temperature": 0.1}
        if not model_id.lower().startswith("gemma-"):
            gen_config["responseMimeType"] = "application/json"
        req_body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_config,
        }
        endpoint = f"{GEMINI_API_BASE}/models/{model_id}:generateContent"

        for api_key in rotated_keys[:3]:
            headers = {
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            }
            try:
                resp = await client.post(endpoint, json=req_body, headers=headers, timeout=35.0)
                if resp.status_code == 200:
                    raw_content = _extract_gemini_text(resp.json())
                    try:
                        return _parse_skeptic_json_and_build_result(
                            fam_id, model_id, raw_content, allowed_citations, allowed_koap
                        )
                    except ValueError as parse_exc:
                        last_error = f"ValueError ({model_id}): {parse_exc}"
                        break
                last_error = f"HTTP {resp.status_code} ({model_id})"
                if resp.status_code in (400, 404):
                    break
                if resp.status_code in (429, 503):
                    await asyncio.sleep(0.25)
            except Exception as exc:
                last_error = f"{type(exc).__name__} ({model_id}): {exc}"

    return {
        "family_id": fam_id,
        "status": "UNAVAILABLE",
        "model_used": None,
        "skeptic_verdict": None,
        "confidence": 0.0,
        "strongest_defense_argument_ru": "",
        "verified_hard_facts": [],
        "challenged_findings": [],
        "citation_basis": [],
        "error": last_error,
    }


async def _invoke_single_skeptic_family(
    family: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    api_keys: List[str],
    allowed_citations: Set[str],
    allowed_koap: Set[str],
    client: httpx.AsyncClient,
    key_offset: int = 0,
) -> Dict[str, Any]:
    fam_id = family["family_id"]
    models = family["models"]
    last_error = "no_models_or_keys"

    for model_id in models:
        rotated_keys = api_keys[key_offset % len(api_keys) :] + api_keys[: key_offset % len(api_keys)]
        for api_key in rotated_keys[:3]:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://adradar.ru",
                "X-Title": "AdRadar Automated Reality Verifier",
            }
            req_body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            }
            try:
                resp = await client.post(OPENROUTER_CHAT_ENDPOINT, json=req_body, headers=headers, timeout=45.0)
                if resp.status_code == 200:
                    data = resp.json()
                    msg = (data.get("choices") or [{}])[0].get("message") or {}
                    raw_content = str(msg.get("content") or "").strip()
                    if not raw_content and msg.get("reasoning"):
                        raw_content = str(msg.get("reasoning") or "").strip()
                    try:
                        return _parse_skeptic_json_and_build_result(
                            fam_id, model_id, raw_content, allowed_citations, allowed_koap
                        )
                    except ValueError as parse_exc:
                        last_error = f"ValueError ({model_id}): {parse_exc}"
                        break  # Try next fallback model in this family instead of repeating on keys
                last_error = f"HTTP {resp.status_code} ({model_id})"
                if resp.status_code in (400, 404):
                    break  # Move to next fallback model in this family
                if resp.status_code == 429:
                    await asyncio.sleep(0.3)
            except Exception as exc:
                last_error = f"{type(exc).__name__} ({model_id}): {exc}"

    return {
        "family_id": fam_id,
        "status": "UNAVAILABLE",
        "model_used": None,
        "skeptic_verdict": None,
        "confidence": 0.0,
        "strongest_defense_argument_ru": "",
        "verified_hard_facts": [],
        "challenged_findings": [],
        "citation_basis": [],
        "error": last_error,
    }


async def run_adversarial_skeptic_for_lead(
    company_report: Dict[str, Any],
    proof_matrix: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    api_keys: Optional[List[str]] = None,
    models_config: Optional[Dict[str, Any]] = None,
    provider: str = "gemini",
) -> Dict[str, Any]:
    """
    Runs 2 orthogonal LLM families acting as Defense Attorneys for the audited business
    to challenge weak scanner assumptions and catch subtle false positives.
    Defaults to Direct Google Gemini API (`provider="gemini"`), with optional `provider="openrouter"`.
    """
    prov = str(provider or "gemini").strip().lower()
    if prov == "openrouter":
        keys = api_keys if api_keys is not None else _get_openrouter_keys()
        if not keys:
            return {
                "status": "SKEPTIC_UNAVAILABLE",
                "reason": "Ключи OpenRouter API не найдены.",
                "pii_redacted": False,
                "families": [],
            }
        families = _get_skeptic_model_families(models_config)
        missing_msg = "В config/openrouter_models.yaml не заданы модели ensemble_jury."
        invoker = _invoke_single_skeptic_family
    else:
        keys = api_keys if api_keys is not None else _get_gemini_api_keys()
        if not keys:
            return {
                "status": "SKEPTIC_UNAVAILABLE",
                "reason": "Ключи Gemini API (GEMINI_API_KEY / GOOGLE_API_KEY) не найдены.",
                "pii_redacted": False,
                "families": [],
            }
        families = _get_gemini_skeptic_families(models_config)
        missing_msg = "В config/gemini_models.yaml не заданы модели reality_skeptic_jury."
        invoker = _invoke_single_gemini_skeptic_family

    sanitized_payload, pii_redacted = _build_anonymized_skeptic_payload(company_report, proof_matrix)
    allowed_citations, allowed_koap = _load_whitelist()
    if not families:
        return {
            "status": "SKEPTIC_UNAVAILABLE",
            "reason": missing_msg,
            "pii_redacted": pii_redacted,
            "families": [],
        }

    system_prompt = (
        "Ты — опытный АДВОКАТ ОТВЕТЧИКА в делах ФАС России и Роскомнадзора.\n"
        "Твоя главная цель — РАЗБИТЬ обвинения автоматического сканера и выявить ложные срабатывания (FALSE POSITIVE).\n"
        "Проверяй строго по действующему праву РФ:\n"
        "1) По 38-ФЗ: собственный блог / каталог компании или нейтральный редакционный обзор БЕЗ промокодов, партнерских ref-хвостов и чужого бренда подпадает под исключения п. 2-3 ч. 2 ст. 2 38-ФЗ. Старше 365 дней — истечение давности по ст. 4.5 КоАП РФ.\n"
        "2) По 152-ФЗ: реальными неоспоримыми нарушениями являются активная форма сбора ПД без ссылки на Политику/чекбокса, предпроставленный чекбокс или прямо зафиксированные скрипты google-analytics/googletagmanager/google-forms в HTML.\n"
        "3) СТРОГИЙ ЗАПРЕТ НА ВЫДУМАННЫЕ НОМЕРА ДЕЛ: ЗАПРЕЩЕНО придумывать номера судебных дел (05-..., А40-...) и писем. Ссылайся ТОЛЬКО на статьи ФЗ (38-ФЗ, 152-ФЗ, 156-ФЗ, 168-ФЗ, 248-ФЗ) и КоАП РФ (4.5, 13.11, 13.15, 13.55, 14.3, 14.7, 14.8, 20.3).\n\n"
        "Верни ответ СТРОГО в формате JSON:\n"
        "{\n"
        '  "skeptic_verdict": "UPHOLD_VIOLATION" | "CHALLENGE_BORDERLINE" | "DISMISS_FALSE_POSITIVE",\n'
        '  "confidence": 0.0,\n'
        '  "strongest_defense_argument_ru": "сильнейший довод защиты ответчика по фактам",\n'
        '  "verified_hard_facts": ["неоспоримый факт 1"],\n'
        '  "challenged_findings": ["спорный пункт, который защита оспорит"],\n'
        '  "citation_basis": ["ч. 16 ст. 14.3 КоАП РФ"]\n'
        "}"
    )

    user_prompt = (
        "Перед тобой обезличенное досье аудита и матрица доказательств Stage A.\n"
        "Выступи адвокатом компании-ответчика и оцени, устоит ли хотя бы одно жесткое обвинение в ФАС/РКН:\n"
        f"<untrusted_audit_data>\n{sanitized_payload}\n</untrusted_audit_data>"
    )

    close_client = False
    c = client
    if c is None:
        c = httpx.AsyncClient(timeout=40.0)
        close_client = True

    try:
        tasks = [
            invoker(
                family=fam,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                api_keys=keys,
                allowed_citations=allowed_citations,
                allowed_koap=allowed_koap,
                client=c,
                key_offset=idx,
            )
            for idx, fam in enumerate(families)
        ]
        fam_results = await asyncio.gather(*tasks)
    finally:
        if close_client:
            await c.aclose()

    ok_fams = [r for r in fam_results if r.get("status") == "OK"]
    return {
        "status": "OK" if ok_fams else "SKEPTIC_UNAVAILABLE",
        "pii_redacted": pii_redacted,
        "families": list(fam_results),
        "responding_families_count": len(ok_fams),
    }


def compute_final_consensus(
    proof_matrix: Dict[str, Any],
    skeptic_res: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Combines deterministic Stage A proof matrix with Stage B 2-family skeptic verdicts."""
    stage_a = proof_matrix["stage_a_verdict"]
    if stage_a == "AUTO_REJECTED_FP":
        return {
            "final_verdict": "AUTO_REJECTED_FP",
            "consensus_reason": "; ".join(proof_matrix.get("rejected_reasons") or ["Отклонено детерминированным фильтром Stage A"]),
        }

    if not skeptic_res or skeptic_res.get("status") != "OK":
        final_v = "AUTO_APPROVED_HARD_FACT" if stage_a == "CONFIRMED_HARD_FACT" else "NEEDS_HUMAN_CHECK"
        note = "Stage B (Skeptic) недоступен или отключен — вердикт вынесен по детерминированной матрице Stage A."
        return {"final_verdict": final_v, "consensus_reason": note}

    ok_fams = [f for f in skeptic_res.get("families", []) if f.get("status") == "OK"]
    votes = [f.get("skeptic_verdict") for f in ok_fams]

    if not votes:
        final_v = "AUTO_APPROVED_HARD_FACT" if stage_a == "CONFIRMED_HARD_FACT" else "NEEDS_HUMAN_CHECK"
        return {"final_verdict": final_v, "consensus_reason": "Нет валидных ответов Stage B."}

    if all(v == "DISMISS_FALSE_POSITIVE" for v in votes):
        args_text = " | ".join(
            f"{f.get('family_id')}: {f.get('strongest_defense_argument_ru') or 'DISMISS'}"
            for f in ok_fams
        )
        return {
            "final_verdict": "AUTO_REJECTED_FP",
            "consensus_reason": f"Единогласный отвод адвокатами защиты Stage B ({len(votes)}/{len(votes)}): {args_text[:280]}",
        }

    if (
        stage_a == "CONFIRMED_HARD_FACT"
        and all(v == "UPHOLD_VIOLATION" for v in votes)
        and len(proof_matrix.get("hard_facts") or []) >= 1
    ):
        return {
            "final_verdict": "AUTO_APPROVED_HARD_FACT",
            "consensus_reason": f"Жесткие факты Stage A подтверждены {len(votes)}/{len(votes)} независимыми моделями Stage B.",
        }

    return {
        "final_verdict": "NEEDS_HUMAN_CHECK",
        "consensus_reason": (
            f"Требует быстрой проверки человеком: Stage A={stage_a}, голоса Stage B={', '.join(votes)}."
        ),
    }


def render_reality_verification_html(report: Dict[str, Any]) -> str:
    """Renders a strictly self-contained zero-CDN HTML report of verified hunt leads."""
    summary = report.get("summary") or {}
    leads = report.get("leads") or []
    gen_at = html.escape(str(report.get("generated_at") or ""))
    src_rep = html.escape(str(report.get("source_report_path") or ""))

    badge_colors = {
        "AUTO_APPROVED_HARD_FACT": ("#065f46", "#d1fae5"),
        "NEEDS_HUMAN_CHECK": ("#92400e", "#fef3c7"),
        "AUTO_REJECTED_FP": ("#991b1b", "#fee2e2"),
    }

    rows_html: List[str] = []
    for ld in leads:
        dom = html.escape(str(ld.get("domain") or ""))
        inn = html.escape(str(ld.get("inn") or "не найден"))
        legal_name = html.escape(str(ld.get("legal_name") or dom))
        fv = str(ld.get("final_verdict") or "NEEDS_HUMAN_CHECK")
        fg, bg = badge_colors.get(fv, ("#1f2937", "#f3f4f6"))
        reason = html.escape(str(ld.get("consensus_reason") or ""))

        pm = ld.get("proof_matrix") or {}
        crit_badges = []
        for ck, cv in (pm.get("criteria") or {}).items():
            st = str(cv.get("status") or "WARN")
            c_cls = "pass" if st == "PASS" else ("fail" if st == "FAIL" else "warn")
            short_k = html.escape(ck.split("_", 1)[0])
            crit_badges.append(
                f'<span class="crit {c_cls}" title="{html.escape(str(cv.get("note") or ""))}">{short_k}:{html.escape(st)}</span>'
            )

        hard_items = []
        for hf in (pm.get("hard_facts") or [])[:5]:
            hard_items.append(
                f"<li><b>{html.escape(str(hf.get('signature')))}</b> "
                f"({html.escape(str(hf.get('legal_basis')))}): "
                f"<code>{html.escape(str(hf.get('quote') or hf.get('source_url') or '')[:140])}</code></li>"
            )
        hard_html = f"<ul>{''.join(hard_items)}</ul>" if hard_items else "<span class='muted'>Нет жестких фактов</span>"

        soft_items = []
        for sf in (pm.get("soft_findings") or [])[:4]:
            soft_items.append(
                f"<li>{html.escape(str(sf.get('signature')))} — {html.escape(str(sf.get('soft_reason') or '')[:120])}</li>"
            )
        soft_html = f"<ul>{''.join(soft_items)}</ul>" if soft_items else "<span class='muted'>—</span>"

        sk = ld.get("skeptic") or {}
        sk_parts = []
        for fam in sk.get("families") or []:
            if fam.get("status") == "OK":
                sk_parts.append(
                    f"<div><b>{html.escape(str(fam.get('model_used')))}</b> "
                    f"[{html.escape(str(fam.get('skeptic_verdict')))}]: "
                    f"{html.escape(str(fam.get('strongest_defense_argument_ru') or '')[:220])}</div>"
                )
        sk_html = "".join(sk_parts) if sk_parts else f"<span class='muted'>{html.escape(str(sk.get('reason') or sk.get('status') or 'отключено'))}</span>"

        rows_html.append(
            "<tr>"
            f"<td><b>{dom}</b><br><span class='muted'>{legal_name}<br>ИНН: {inn}</span></td>"
            f"<td><span class='badge' style='color:{fg};background:{bg};'>{html.escape(fv)}</span>"
            f"<div class='reason'>{reason}</div></td>"
            f"<td><div class='crit-grid'>{''.join(crit_badges)}</div></td>"
            f"<td>{hard_html}</td>"
            f"<td>{soft_html}</td>"
            f"<td>{sk_html}</td>"
            "</tr>"
        )

    table_body = "\n".join(rows_html) if rows_html else "<tr><td colspan='6'>Лиды в отчете отсутствуют.</td></tr>"
    return (
        "<!DOCTYPE html>\n"
        "<html lang='ru'>\n<head>\n<meta charset='utf-8'>\n"
        "<title>AdRadar — Отчет автоматической верификации реальности</title>\n"
        "<style>\n"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f8fafc;color:#0f172a;margin:24px;}\n"
        ".cards{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:12px;margin:16px 0;}\n"
        ".card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;}\n"
        ".card .num{font-size:26px;font-weight:700;margin-top:4px;}\n"
        "table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;font-size:13px;}\n"
        "th,td{border:1px solid #e2e8f0;padding:10px;text-align:left;vertical-align:top;}\n"
        "th{background:#f1f5f9;font-weight:600;}\n"
        ".badge{display:inline-block;padding:4px 8px;border-radius:6px;font-weight:700;font-size:12px;}\n"
        ".reason{font-size:12px;color:#334155;margin-top:6px;}\n"
        ".muted{color:#64748b;font-size:12px;}\n"
        ".crit-grid{display:flex;flex-wrap:wrap;gap:4px;}\n"
        ".crit{padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;}\n"
        ".crit.pass{background:#dcfce7;color:#166534;}\n"
        ".crit.warn{background:#fef9c3;color:#854d0e;}\n"
        ".crit.fail{background:#fee2e2;color:#991b1b;}\n"
        "ul{margin:0;padding-left:16px;} li{margin-bottom:4px;}\n"
        "</style>\n</head>\n<body>\n"
        "<h1>AdRadar — Автоматическая проверка реальности нарушений (Stage A + Stage B)</h1>\n"
        f"<div class='muted'>Сформировано: {gen_at} | Источник: <code>{src_rep}</code></div>\n"
        "<div class='cards'>"
        f"<div class='card'><div class='muted'>Проверено компаний</div><div class='num'>{int(summary.get('total_leads_checked', 0))}</div></div>"
        f"<div class='card'><div class='muted'>Подтверждено (Hard Fact)</div><div class='num' style='color:#065f46'>{int(summary.get('auto_approved_hard_fact', 0))}</div></div>"
        f"<div class='card'><div class='muted'>На ручную проверку</div><div class='num' style='color:#92400e'>{int(summary.get('needs_human_check', 0))}</div></div>"
        f"<div class='card'><div class='muted'>Отклонено (False Positive)</div><div class='num' style='color:#991b1b'>{int(summary.get('auto_rejected_fp', 0))}</div></div>"
        "</div>\n"
        "<table>\n<thead><tr>"
        "<th>Компания / ИНН</th><th>Вердикт консенсуса</th><th>Матрица C1–C6</th>"
        "<th>Жесткие факты (Witness)</th><th>Мягкие / спорные</th><th>Адвокат защиты (Stage B)</th>"
        "</tr></thead>\n<tbody>\n"
        f"{table_body}\n"
        "</tbody>\n</table>\n</body>\n</html>\n"
    )


async def verify_hunt_leads_reality(
    report_path: Optional[Path] = None,
    with_skeptic: bool = True,
    max_leads: int = 15,
    output_dir: Optional[Path] = None,
    with_visual_qc: bool = False,
    skeptic_client: Optional[httpx.AsyncClient] = None,
    api_keys: Optional[List[str]] = None,
    provider: str = "gemini",
) -> Dict[str, Any]:
    """
    Runs end-to-end Stage A + Stage B verification over `hunt_vcru_consolidated_report.json`.
    Writes `reality_verification_report.json`, self-contained `reality_verification_report.html`,
    and appends records into `feedback/auto_skeptic_feedback.jsonl`.
    """
    active_data_dir = _active_data_dir()
    if report_path is None:
        cand1 = active_data_dir / "reports" / "vcru_hunt_grouped" / "hunt_vcru_consolidated_report.json"
        cand2 = active_data_dir / "reports" / "hunt_vcru_consolidated_report.json"
        resolved_report = cand1 if cand1.exists() else cand2
    else:
        resolved_report = Path(report_path)

    if not resolved_report.exists():
        raise FileNotFoundError(f"Отчет hunt-vcru не найден: {resolved_report}")

    raw_report = json.loads(resolved_report.read_text(encoding="utf-8"))
    company_reports = list(raw_report.get("consolidated_company_reports") or [])
    if max_leads > 0:
        company_reports = company_reports[:max_leads]

    out_dir = Path(output_dir) if output_dir else (active_data_dir / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    feedback_dir = active_data_dir / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / "auto_skeptic_feedback.jsonl"

    prov = str(provider or "gemini").strip().lower()
    models_cfg = _load_openrouter_models_config() if prov == "openrouter" else _load_gemini_models_config()
    verified_leads: List[Dict[str, Any]] = []

    close_client = False
    c = skeptic_client
    if with_skeptic and c is None:
        c = httpx.AsyncClient(timeout=40.0)
        close_client = True

    try:
        for comp in company_reports:
            proof_matrix = evaluate_lead_proof_matrix(comp)
            skeptic_res: Optional[Dict[str, Any]] = None
            if not with_skeptic:
                skeptic_res = {"status": "DISABLED", "reason": "Stage B отключен флагом --no-skeptic", "families": []}
            elif proof_matrix["stage_a_verdict"] == "AUTO_REJECTED_FP":
                skeptic_res = {"status": "SKIPPED_STAGE_A_REJECTED", "reason": "Отклонено детерминированной проверкой Stage A", "families": []}
            else:
                skeptic_res = await run_adversarial_skeptic_for_lead(
                    company_report=comp,
                    proof_matrix=proof_matrix,
                    client=c,
                    api_keys=api_keys,
                    models_config=models_cfg,
                    provider=prov,
                )

            consensus = compute_final_consensus(proof_matrix, skeptic_res)
            verified_leads.append({
                "domain": proof_matrix["domain"],
                "inn": proof_matrix["inn"],
                "legal_name": proof_matrix["legal_name"],
                "final_verdict": consensus["final_verdict"],
                "consensus_reason": consensus["consensus_reason"],
                "proof_matrix": proof_matrix,
                "skeptic": skeptic_res,
                "lead_package": comp.get("lead_package"),
            })
    finally:
        if close_client and c is not None:
            await c.aclose()

    visual_qc_summary: Optional[Dict[str, Any]] = None
    if with_visual_qc:
        _ensure_vlm_import_path()
        try:
            from scripts.run_visual_qc import run_batch_from_hunt
            _, visual_qc_summary = await run_batch_from_hunt(
                hunt_report_path=resolved_report,
                max_leads=min(5, max_leads),
            )
        except Exception as e:
            visual_qc_summary = {"error": str(e)}

    approved_cnt = sum(1 for x in verified_leads if x["final_verdict"] == "AUTO_APPROVED_HARD_FACT")
    human_cnt = sum(1 for x in verified_leads if x["final_verdict"] == "NEEDS_HUMAN_CHECK")
    rejected_cnt = sum(1 for x in verified_leads if x["final_verdict"] == "AUTO_REJECTED_FP")

    result: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "source_report_path": str(resolved_report),
        "provider": prov,
        "with_skeptic": with_skeptic,
        "summary": {
            "total_leads_checked": len(verified_leads),
            "auto_approved_hard_fact": approved_cnt,
            "needs_human_check": human_cnt,
            "auto_rejected_fp": rejected_cnt,
        },
        "leads": verified_leads,
        "visual_qc_summary": visual_qc_summary,
    }

    json_out = out_dir / "reality_verification_report.json"
    html_out = out_dir / "reality_verification_report.html"
    result["report_json_path"] = str(json_out)
    result["report_html_path"] = str(html_out)
    result["feedback_jsonl_path"] = str(feedback_path)

    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    html_out.write_text(render_reality_verification_html(result), encoding="utf-8")

    with open(feedback_path, "a", encoding="utf-8") as fb:
        for ld in verified_leads:
            entry = {
                "timestamp": result["generated_at"],
                "domain": ld["domain"],
                "inn": ld["inn"],
                "final_verdict": ld["final_verdict"],
                "stage_a_verdict": (ld["proof_matrix"] or {}).get("stage_a_verdict"),
                "hard_facts_signatures": [h.get("signature") for h in (ld["proof_matrix"] or {}).get("hard_facts", [])],
                "rejected_reasons": (ld["proof_matrix"] or {}).get("rejected_reasons", []),
                "skeptic_status": (ld["skeptic"] or {}).get("status"),
                "skeptic_votes": [
                    {
                        "model": f.get("model_used"),
                        "verdict": f.get("skeptic_verdict"),
                        "argument": f.get("strongest_defense_argument_ru"),
                    }
                    for f in (ld["skeptic"] or {}).get("families", [])
                    if f.get("status") == "OK"
                ],
            }
            fb.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return result
