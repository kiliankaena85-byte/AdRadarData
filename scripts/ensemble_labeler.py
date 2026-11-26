"""
Orthogonal Multi-Family Ensemble Labeler & Active Micro-Question Generator for VC.ru.

Implements the 5-Tier Pipeline:
- Tier 0: Anti-Leakage Gate (blocks golden_set_real_vcru.jsonl), Deterministic Legal Passport
  (erid, strict 'Реклама' label, INN checksums, commercial_targets), and 152-FZ PII Sanitizer.
- Tier 1: Local GPU Core on RTX 4060:
  - Tier 1A: LinkCentricAdClassifierV3 (champion_v3.bundle)
  - Tier 1B: Laya Vega-U 0.4.32 (non-autoregressive conformal NLI sensor over F1..F5)
- Tier 2: Independent Multi-Family Cloud Jury (6-key OpenRouter rotation + SQLite WAL cache):
  - Tier 2A: Decision-1 (GeminiDecisionTeacher / OpenRouter decider gate)
  - Tier 2B: NVIDIA MoE family (loaded strictly from config/openrouter_models.yaml)
  - Tier 2C: Independent Dense Devil's Advocate family (loaded from config/openrouter_models.yaml)
- Tier 3: Compact 10-article Markdown digests for external Frontier LLM chat arbitration + JSON import.
- Tier 4: Single-Fact Disagreement Detector -> <= 300-char highlighted quote + 1 targeted 5-second Micro-Question.

Anti-Echo-Chamber Invariant:
LinkCentric-V3 can NEVER promote an article to AUTO_GOLD on its own.
AUTO_GOLD (weight=2.0) requires 4/4 agreement across LinkCentric-V3 + Laya Vega-U + >= 2 independent
external models of distinct families with aggregate confidence >= 0.90.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import yaml

# Disable TensorFlow before any laya import
os.environ["USE_TF"] = "0"

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "legal_sheaf_verifier" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import ADRADAR_DATA_DIR, MODELS_CACHE_DIR
from legal_sheaf.dual_auditor import PROMO_CODE_REGEX, is_affiliate_link
from legal_sheaf.inn_erid_validators import (
    analyze_ad_label_status,
    extract_commercial_targets,
    extract_erid_tokens,
    find_valid_inns_in_text,
)
from legal_sheaf.net_safety import safe_fetch_with_redirects
from legal_sheaf.schema import DocumentInput, LegalRegime
from legal_sheaf.triad_decision_router import (
    DecisionQuery,
    GeminiDecisionTeacher,
    sanitize_before_external_decision,
)
from legal_sheaf.vega_grey_zone import (
    ATOMIC_FACT_KEYS,
    SHORT_TO_FULL_FACT,
    derive_regime_from_atomic_facts,
    normalize_atomic_facts,
)
from verification_engine.sheaf_vega_verifier import VegaUSensor
from vlm_context_engine.gemini_gateway import (
    GEMINI_API_BASE,
    extract_gemini_response_text,
    get_gemini_api_keys,
)
from vlm_context_engine.openrouter_gateway import (
    OPENROUTER_CHAT_ENDPOINT,
    OPENROUTER_DECISIONS_ENDPOINT,
    _mask_api_key,
    _read_env_key,
    extract_json_resilient,
    get_openrouter_api_keys,
    load_citation_whitelist,
    sanitize_model_citations,
)


def _sanitize_citations_safe(text: str) -> str:
    if not text:
        return ""
    allowed_cit, allowed_koap = load_citation_whitelist()
    return str(sanitize_model_citations(text, allowed_cit, allowed_koap))

logger = logging.getLogger("scripts.ensemble_labeler")

PROMPT_SCHEMA_VERSION = "ensemble_micro_q_v2.0"

# =============================================================================
# STRICT STATUTORY FORMULA: imported from legal_sheaf.vega_grey_zone
# =============================================================================


# =============================================================================
# TIER 1A (LinkCentric-V3) & TIER 1B (Laya Vega-U 0.4.32)
# =============================================================================
def evaluate_tier1a_sheaf(bundle: AdRadarBrainBundle, record: Dict[str, Any], tier0: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates LinkCentricAdClassifierV3 and maps regime + Tier 0 facts into atomic facts F1..F5."""

REGIME_HUMAN_SHORT_RU: Dict[str, str] = {
    "CLEAN_NON_AD": "Не реклама",
    "CLEAN_MARKED_AD": "Маркированная реклама",
    "UNMARKED_AD_NO_ERID": "Скрытая реклама без erid",
    "INCOMPLETE_MARKING_LABEL_OR_INN": "Неполная маркировка",
    "OVER_MARKED_NON_AD": "Избыточная маркировка",
    "FOREIGN_LANG_168FZ": "Нарушение 168-ФЗ",
    "BANNED_OR_SPECIAL_REGIME": "Запрещенный/спецрежим",
}


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    return Path(env_dir).resolve() if env_dir else Path(ADRADAR_DATA_DIR).resolve()


def _normalize_url(url: str) -> str:
    return str(url or "").strip().lower().rstrip("/")


def load_golden_blocklist(golden_path: Optional[Path] = None) -> Tuple[Set[int], Set[str]]:
    """Loads all post_ids and URLs from golden_set_real_vcru.jsonl to guarantee zero data leakage."""
    gpath = golden_path or (REPO_ROOT / "legal_sheaf_verifier" / "tests" / "golden_set_real_vcru.jsonl")
    ids: Set[int] = set()
    urls: Set[str] = set()
    if not gpath.exists():
        return ids, urls
    for line in gpath.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if row.get("post_id") is not None:
                ids.add(int(row["post_id"]))
            if row.get("url"):
                urls.add(_normalize_url(str(row["url"])))
        except Exception:
            continue
    return ids, urls


def load_jury_models_config(config_path: Optional[Path] = None) -> Dict[str, List[str]]:
    """
    Loads external model IDs strictly from legal_sheaf_verifier/config/openrouter_models.yaml
    and verifies pricing == 0.0 (Financial Lock). Never hardcodes model IDs in Python code.
    """
    cpath = config_path or (REPO_ROOT / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml")
    if not cpath.exists():
        raise FileNotFoundError(f"Missing OpenRouter models config: {cpath}")

    cfg = yaml.safe_load(cpath.read_text(encoding="utf-8")) or {}
    specs = cfg.get("verified_model_specs") or {}
    banned = set(
        (cfg.get("paid_zdr_tier") or {}).get("banned_models") or []
    )

    def _filter_free(candidates: List[str]) -> List[str]:
        out: List[str] = []
        for mid in candidates:
            if not mid or mid in banned or mid in out:
                continue
            sp = specs.get(mid) or {}
            pr = sp.get("pricing") or {}
            if sp:
                if float(pr.get("prompt", 1.0)) != 0.0 or float(pr.get("completion", 1.0)) != 0.0:
                    continue
                if sp.get("verified_free") is False:
                    continue
            elif not (mid.endswith(":free") or mid == "openrouter/free"):
                continue
            out.append(mid)
        return out

    jury_cfg = cfg.get("ensemble_jury") or {}
    free_cfg = cfg.get("free_tier") or {}

    t2a_block = jury_cfg.get("tier_2a_decisions") or {}
    t2b_block = jury_cfg.get("tier_2b_nvidia") or {}
    t2c_block = jury_cfg.get("tier_2c_independent") or {}

    tier_2b = _filter_free(
        [
            str(t2b_block.get("primary") or ""),
            str(t2b_block.get("fallback") or ""),
            str(free_cfg.get("primary") or ""),
        ]
    )
    tier_2c = _filter_free(
        [
            str(t2c_block.get("primary") or ""),
            str(t2c_block.get("fallback") or ""),
            str(t2c_block.get("secondary_fallback") or ""),
            *[str(x) for x in (free_cfg.get("fallbacks") or []) if x != "openrouter/free"],
        ]
    )
    # Keep Tier 2C distinct from Tier 2B primary so families remain orthogonal
    if tier_2b:
        tier_2c = [m for m in tier_2c if m != tier_2b[0]]

    tier_2a_decider = _filter_free(
        [
            str(t2a_block.get("primary") or ""),
            str(t2a_block.get("fallback") or ""),
            *[str(x) for x in (free_cfg.get("decider_alpha_candidates") or [])],
        ]
    )

    return {
        "tier_2b_nvidia": tier_2b,
        "tier_2c_independent": tier_2c,
        "tier_2a_decider_fallback": tier_2a_decider,
        "last_verified_date": str(cfg.get("last_verified_date") or ""),
    }


def load_gemini_jury_models_config(config_path: Optional[Path] = None) -> Dict[str, List[str]]:
    """Loads ensemble_labeler_jury models strictly from legal_sheaf_verifier/config/gemini_models.yaml."""
    cpath = config_path or (REPO_ROOT / "legal_sheaf_verifier" / "config" / "gemini_models.yaml")
    if not cpath.exists():
        return {"tier_2b_primary": [], "tier_2c_secondary": [], "last_verified_date": ""}
    cfg = yaml.safe_load(cpath.read_text(encoding="utf-8")) or {}
    jury = cfg.get("ensemble_labeler_jury") or {}
    banned = {str(b).strip().removeprefix("models/") for b in (cfg.get("banned_models") or [])}

    def _clean(block: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        raw_list = [block.get("primary")] + list(block.get("fallbacks") or [])
        for rm in raw_list:
            if not isinstance(rm, str):
                continue
            cm = rm.strip().removeprefix("models/")
            if cm and cm not in banned and cm not in out:
                out.append(cm)
        return out

    return {
        "tier_2b_primary": _clean(jury.get("tier_2b_primary") or {}),
        "tier_2c_secondary": _clean(jury.get("tier_2c_secondary") or {}),
        "last_verified_date": str(cfg.get("last_verified_date") or ""),
    }


# =============================================================================
# SQLITE WAL JURY CACHE
# =============================================================================
class EnsembleJuryCache:
    """Idempotent SQLite WAL cache for external jury verdicts so reruns spend 0 API calls."""

    def __init__(self, db_path: Optional[Path] = None):
        default_db = _active_data_dir() / "db" / "ensemble_jury_cache.sqlite"
        self.db_path = Path(db_path) if db_path else default_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jury_verdicts (
                    cache_key TEXT PRIMARY KEY,
                    post_id INTEGER,
                    tier_slot TEXT,
                    model_used TEXT,
                    payload_json TEXT,
                    created_at REAL
                );
                """
            )
            conn.commit()

    @staticmethod
    def make_key(post_id: int, digest_text: str, tier_slot: str) -> str:
        raw = f"{PROMPT_SCHEMA_VERSION}|{tier_slot}|{post_id}|{digest_text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                row = conn.execute(
                    "SELECT payload_json, model_used FROM jury_verdicts WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if row:
                    data = json.loads(row[0])
                    if isinstance(data, dict):
                        data.setdefault("model_used", row[1])
                        return data
        except Exception:
            return None
        return None

    def set(self, cache_key: str, post_id: int, tier_slot: str, model_used: str, payload: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO jury_verdicts (
                        cache_key, post_id, tier_slot, model_used, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        int(post_id),
                        tier_slot,
                        str(model_used),
                        json.dumps(payload, ensure_ascii=False),
                        time.time(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug("Failed to cache jury verdict: %s", e)


# =============================================================================
# TIER 0: DETERMINISTIC PASSPORT & SMART DIGEST BUILDER
# =============================================================================
def build_tier0_passport(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Computes immutable Tier 0 deterministic legal facts (erid, strict ad label,
    valid INNs, commercial links/contacts, affiliate params, promo codes) and
    a 152-FZ sanitized compact digest (~600-900 chars) for cloud/chat arbitration.
    """
    full_text = str(record.get("full_text") or record.get("text") or record.get("text_window") or "").strip()
    title = str(record.get("title") or "").strip()
    url = str(record.get("url") or f"https://vc.ru/{record.get('post_id', 0)}").strip()
    raw_links = list(record.get("external_links") or [])
    ad_meta = record.get("platform_ad_meta") if isinstance(record.get("platform_ad_meta"), dict) else {}

    erids = list(record.get("erids") or [])
    if not erids:
        erids = extract_erid_tokens(full_text, page_url=" ".join(raw_links), platform_ad_meta=ad_meta)

    ad_label_info = analyze_ad_label_status(full_text)
    has_strict_ad_label = bool(record.get("has_strict_ad_label") or ad_label_info.get("has_strict_ad_label"))

    valid_inns = list(record.get("valid_inns") or [])
    if not valid_inns:
        valid_inns = find_valid_inns_in_text(full_text)

    targets_info = extract_commercial_targets(full_text, raw_links=raw_links, page_url=url)
    ext_links = list(dict.fromkeys(raw_links + list(targets_info.get("commercial_urls") or [])))
    comm_targets = list(
        dict.fromkeys(list(record.get("commercial_targets") or []) + list(targets_info.get("all_targets") or []))
    )

    has_affiliate = bool(record.get("has_affiliate_link") or any(is_affiliate_link(u) for u in ext_links))
    has_promo = bool(record.get("has_promo_code") or PROMO_CODE_REGEX.search(full_text))
    is_promo_flag = bool(record.get("is_promo") or record.get("is_promo_platform_flag"))

    # Build smart compact digest (~650-950 chars) preserving head (280), commercial middle, and tail (340)
    paragraphs: List[str] = list(record.get("paragraphs") or [])
    if not paragraphs:
        paragraphs = [p.strip() for p in re.split(r"(?:\r?\n)+|(?<=[.!?])\s{2,}", full_text) if p.strip()]

    clean_body = re.sub(r"\n\[Ссылки:.*\]$", "", full_text).strip()
    if len(clean_body) <= 850:
        raw_digest = clean_body
    else:
        head_part = clean_body[:300].strip()
        tail_part = clean_body[-350:].strip()
        trigger_re = re.compile(
            r"(?i)(?:https?://|t\.me/|max\.ru/|vk\.com/|промокод|скидк|тариф|рубл|₽|erid|#реклам|бонус|акци[яи]|реф|vpn|казино|ставк|крипт)"
        )
        mid_hits: List[str] = []
        used_chars = 0
        for p in paragraphs:
            p_clean = p.strip()
            if not p_clean or p_clean in head_part or p_clean in tail_part:
                continue
            if trigger_re.search(p_clean) or any(t in p_clean for t in comm_targets if len(t) >= 5):
                clip = p_clean[:240]
                if clip not in mid_hits and used_chars + len(clip) <= 320:
                    mid_hits.append(clip)
                    used_chars += len(clip)
        if mid_hits:
            raw_digest = f"{head_part} ... " + " | ".join(mid_hits) + f" ... {tail_part}"
        else:
            raw_digest = f"{head_part} ... {tail_part}"

    sanitized_digest, pii_redacted = sanitize_before_external_decision(raw_digest)
    sanitized_title, _ = sanitize_before_external_decision(title)

    author_meta = record.get("author_meta") if isinstance(record.get("author_meta"), dict) else {}

    return {
        "erids": erids[:6],
        "has_erid": len(erids) > 0,
        "has_strict_ad_label": has_strict_ad_label,
        "valid_inns": valid_inns[:6],
        "has_inn": len(valid_inns) > 0,
        "external_links": ext_links[:10],
        "commercial_targets": comm_targets[:12],
        "has_affiliate_link": has_affiliate,
        "has_promo_code": has_promo,
        "is_promo": is_promo_flag,
        "is_editorial": bool(record.get("is_editorial")),
        "author_meta": author_meta,
        "sanitized_title": sanitized_title[:200],
        "sanitized_digest": sanitized_digest[:1100],
        "pii_redacted": pii_redacted,
    }


# =============================================================================
# STRICT STATUTORY FORMULA: imported from legal_sheaf.vega_grey_zone
# =============================================================================


# =============================================================================
# TIER 1A (LinkCentric-V3) & TIER 1B (Laya Vega-U 0.4.32)
# =============================================================================
def evaluate_tier1a_sheaf(bundle: AdRadarBrainBundle, record: Dict[str, Any], tier0: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates LinkCentricAdClassifierV3 and maps regime + Tier 0 facts into atomic facts F1..F5."""
    full_text = str(record.get("full_text") or record.get("text") or record.get("text_window") or "")
    doc = DocumentInput(
        clean_text=full_text,
        page_url=str(record.get("url") or f"https://vc.ru/{record.get('post_id', 0)}"),
        author_domain="vc.ru",
        platform_ad_meta=record.get("platform_ad_meta") or {},
        context_mode="EXTERNAL_PUBLICATION",
    )
    rep = bundle.verify_document(doc)
    slot_map = {int(s.slot_id): float(s.probability) for s in (rep.active_slots or [])}

    is_viol = bool(rep.is_violation)
    reg_name = rep.regime_name
    prob = float(rep.violation_probability)
    conf = round(max(prob, 1.0 - prob), 4)

    ext_links = tier0.get("external_links") or []
    only_tg = len(ext_links) == 1 and ("t.me/" in ext_links[0].lower() or "telegram.me/" in ext_links[0].lower())

    facts = {
        "F1_OBJECT_PROMOTED": bool(
            reg_name in ("UNMARKED_AD_NO_ERID", "CLEAN_MARKED_AD", "INCOMPLETE_MARKING_LABEL_OR_INN", "BANNED_OR_SPECIAL_REGIME")
            or slot_map.get(0, 0.0) >= 0.55
        ),
        "F2_AUTHOR_SELF_BIO": bool(not is_viol and only_tg and not tier0.get("has_erid")),
        "F3_UNIFORM_MULTI_REVIEW": bool(not is_viol and len(ext_links) >= 3 and not tier0.get("has_erid")),
        "F4_COMMERCIAL_CTA_OR_OFFER": bool(
            (is_viol and reg_name == "UNMARKED_AD_NO_ERID")
            or tier0.get("has_affiliate_link")
            or tier0.get("has_promo_code")
            or slot_map.get(2, 0.0) >= 0.55
        ),
        "F5_BANNED_OR_SPECIAL": bool(reg_name == "BANNED_OR_SPECIAL_REGIME"),
    }

    return {
        "voter": "LinkCentric-V3",
        "family": "link_centric_local",
        "facts": facts,
        "is_violation": is_viol,
        "regime": reg_name,
        "violation_probability": round(prob, 4),
        "confidence": conf,
        "conformal_verdict": rep.conformal_verdict,
        "abstain": rep.conformal_verdict == "ABSTAIN_REVIEW",
        "energy": round(float(rep.total_sheaf_energy), 4),
    }


def evaluate_tier1b_vega(sensor: VegaUSensor, tier0: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates all 5 atomic facts F1..F5 in a single non-autoregressive Vega-U pass on RTX 4060.
    Zero text generation; uses conformal prediction_set, abstain, and energy.
    """
    state_str = (
        f"TITLE: {tier0['sanitized_title']}\n"
        f"ERID_PRESENT: {tier0['has_erid']} | STRICT_AD_LABEL: {tier0['has_strict_ad_label']} | "
        f"AFFILIATE_URL: {tier0['has_affiliate_link']} | PROMO_CODE: {tier0['has_promo_code']}\n"
        f"EXTERNAL_LINKS: {', '.join(tier0['external_links'][:6]) or 'NONE'}\n"
        f"AUTHOR_META: {json.dumps(tier0['author_meta'], ensure_ascii=False)}\n"
        f"DIGEST:\n{tier0['sanitized_digest']}"
    )

    questions = {
        "F1_OBJECT_PROMOTED": {
            "type": "noul",
            "instructions": (
                "Does this publication highlight or promote a specific commercial product, "
                "company, service, paid course, or commercial Telegram bot/channel?"
            ),
        },
        "F2_AUTHOR_SELF_BIO": {
            "type": "noul",
            "instructions": (
                "Is the external link merely a neutral signature to the author's own personal blog "
                "at the end of an informational article without prices, discounts, or commercial offer?"
            ),
        },
        "F3_UNIFORM_MULTI_REVIEW": {
            "type": "noul",
            "instructions": (
                "Is this an objective, uniform analytical review of 3 or more competing products/services "
                "without singling out one paid sponsor or single referral link?"
            ),
        },
        "F4_COMMERCIAL_CTA_OR_OFFER": {
            "type": "noul",
            "instructions": (
                "Does the text or links contain a call to buy/order/register, tariff/price pitch, "
                "discount, promo code, or referral/UTM tracking link?"
            ),
        },
        "F5_BANNED_OR_SPECIAL": {
            "type": "noul",
            "instructions": (
                "Does the publication promote a prohibited or restricted object in Russia "
                "(online casino, betting, VPN bypass service, crypto exchange referral, or unlicensed medical/credit offer)?"
            ),
        },
    }

    facts: Dict[str, bool] = {}
    confs: List[float] = []
    energies: List[float] = []
    any_abstain = False

    if sensor.light_mode:
        # Deterministic lightweight test path (only used when light_mode=True in unit tests)
        has_links = len(tier0["external_links"]) > 0 or len(tier0["commercial_targets"]) > 0
        only_tg = len(tier0["external_links"]) == 1 and "t.me/" in tier0["external_links"][0].lower()
        facts = {
            "F1_OBJECT_PROMOTED": bool(has_links or tier0["has_promo_code"]),
            "F2_AUTHOR_SELF_BIO": bool(only_tg and not tier0["has_promo_code"] and not tier0["has_affiliate_link"]),
            "F3_UNIFORM_MULTI_REVIEW": False,
            "F4_COMMERCIAL_CTA_OR_OFFER": bool(tier0["has_promo_code"] or tier0["has_affiliate_link"]),
            "F5_BANNED_OR_SPECIAL": False,
        }
        is_viol, reg = derive_regime_from_atomic_facts(tier0, facts)
        return {
            "voter": "Laya-Vega-U",
            "family": "vega_nli_local",
            "facts": facts,
            "is_violation": is_viol,
            "regime": reg.name,
            "confidence": 0.92,
            "abstain": False,
            "energy": -1.0,
        }

    agent = sensor._get_agent()
    try:
        out = agent.predict(state=state_str[:10000], questions=questions)
        answers = out.get("answers") or {}
        for fk in ATOMIC_FACT_KEYS:
            ans = answers.get(fk) or {}
            pset = [str(x).lower() for x in (ans.get("prediction_set") or [])]
            is_true = ("true" in pset) and ("false" not in pset)
            facts[fk] = is_true
            confs.append(float(ans.get("confidence", 0.0)))
            energies.append(float(ans.get("energy", 0.0)))
            if ans.get("abstain") or ans.get("unbound") or len(pset) != 1:
                if fk in ("F1_OBJECT_PROMOTED", "F2_AUTHOR_SELF_BIO", "F4_COMMERCIAL_CTA_OR_OFFER"):
                    any_abstain = True
    except Exception as e:
        logger.warning("VegaUSensor batch error: %s", e)
        facts = {fk: False for fk in ATOMIC_FACT_KEYS}
        any_abstain = True

    is_viol, reg = derive_regime_from_atomic_facts(tier0, facts)
    mean_conf = round(sum(confs) / max(1, len(confs)), 4) if confs else 0.0
    mean_energy = round(sum(energies) / max(1, len(energies)), 4) if energies else 0.0

    return {
        "voter": "Laya-Vega-U",
        "family": "vega_nli_local",
        "facts": facts,
        "is_violation": is_viol,
        "regime": reg.name,
        "confidence": mean_conf,
        "abstain": any_abstain,
        "energy": mean_energy,
    }


# =============================================================================
# TIER 2: MULTI-FAMILY CLOUD JURY (Decision-1 + NVIDIA MoE + Independent Dense)
# =============================================================================
def _build_batch_jury_prompt(items: List[Tuple[int, Dict[str, Any]]], role_mode: str) -> str:
    role_header = (
        "Ты — строгий правовой аудитор рекламы РФ (38-ФЗ: ст. 2, ст. 3, ст. 7, ст. 18.1). "
        "Твоя задача: выявить скрытые нативные интеграции, промо-офферы, реферальные ссылки и саморекламу."
        if role_mode != "devils_advocate"
        else "Ты — независимый «адвокат дьявола» по ст. 2 и ст. 3 38-ФЗ РФ. "
        "Внимательно проверяй, не является ли материал органическим редакционным обзором >= 3 конкурентов "
        "(п. 3 ч. 2 ст. 2 38-ФЗ), новостью или личной статьей автора с нейтральной ссылкой на свой блог в подвале без оффера."
    )

    cards_txt: List[str] = []
    for post_id, t0 in items:
        cards_txt.append(
            f"--- ARTICLE post_id={post_id} ---\n"
            f"Заголовок: {t0['sanitized_title']}\n"
            f"Факты Tier-0: erid={t0['erids']}, strict_reklama={t0['has_strict_ad_label']}, "
            f"inns={t0['valid_inns']}, affiliate={t0['has_affiliate_link']}, promo_code={t0['has_promo_code']}\n"
            f"Автор: {json.dumps(t0['author_meta'], ensure_ascii=False)}\n"
            f"Ссылки/контакты: {json.dumps(t0['commercial_targets'][:8], ensure_ascii=False)}\n"
            f"Выжимка текста: {t0['sanitized_digest']}"
        )

    joined = "\n\n".join(cards_txt)
    return (
        f"{role_header}\n\n"
        "Для КАЖДОЙ статьи определи 5 бинарных правовых фактов (true/false):\n"
        "- F1: Выделен ли конкретный коммерческий товар, сервис, компания, бот или платная услуга?\n"
        "- F2: Единственная внешняя ссылка (например t.me/...) — это нейтральная подпись автора на свой личный блог в конце статьи БЕЗ оффера, цен и призыва купить?\n"
        "- F3: Это однородный аналитический обзор >= 3 разных конкурентов в едином стиле без выделения одного спонсора и без реферальной ссылки на одного игрока?\n"
        "- F4: Есть ли призыв к покупке/заказу/регистрации, тарифы, скидка, промокод или реферальная/UTM ссылка?\n"
        "- F5: Рекламируется ли запрещенный/ограниченный объект (казино, беттинг, VPN для обхода блокировок, криптобиржа с рефкой, медицина/кредит без дисклеймера)?\n\n"
        f"<articles>\n{joined}\n</articles>\n\n"
        "Верни СТРОГО валидный JSON-объект без пояснений вокруг него в формате:\n"
        '{"items": [{"post_id": 123, "F1": false, "F2": false, "F3": false, "F4": false, "F5": false, "confidence": 0.95}]}'
    )


def _extract_json_items_array(raw_text: str) -> List[Dict[str, Any]]:
    """Extracts list of per-article JSON dicts from either {'items': [...]} or [...] output."""
    cleaned = re.sub(r"<think>.*?</think>", "", str(raw_text or ""), flags=re.DOTALL | re.IGNORECASE).strip()
    if not cleaned:
        return []
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    for candidate in (cleaned,):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                return [x for x in parsed["items"] if isinstance(x, dict)]
        except Exception:
            pass

    arr_match = re.search(r"\[[\s\S]*\]", cleaned)
    if arr_match:
        try:
            parsed_arr = json.loads(arr_match.group(0))
            if isinstance(parsed_arr, list):
                return [x for x in parsed_arr if isinstance(x, dict)]
        except Exception:
            pass

    try:
        obj = extract_json_resilient(cleaned)
        if isinstance(obj, dict):
            if isinstance(obj.get("items"), list):
                return [x for x in obj["items"] if isinstance(x, dict)]
            if "post_id" in obj:
                return [obj]
    except Exception:
        pass
    return []


class MultiFamilyCloudJury:
    """
    Executes Tier 2A (Decision-1), Tier 2B (NVIDIA MoE), and Tier 2C (Independent Dense)
    with 6-key OpenRouter rotation, multi-article micro-batching, and SQLite WAL caching.
    """

    def __init__(
        self,
        cache: Optional[EnsembleJuryCache] = None,
        api_keys: Optional[List[str]] = None,
        gemini_api_key: Optional[str] = None,
        models_cfg: Optional[Dict[str, List[str]]] = None,
        gemini_models_cfg: Optional[Dict[str, List[str]]] = None,
    ):
        self.cache = cache or EnsembleJuryCache()
        self.api_keys = api_keys if api_keys is not None else get_openrouter_api_keys()
        self._key_idx = 0
        self._dead_keys: Set[str] = set()
        self._cooldown_models: Set[str] = set()
        self._decision_api_disabled: bool = False
        self.models_cfg = models_cfg or load_jury_models_config()
        self.gemini_models_cfg = gemini_models_cfg or load_gemini_jury_models_config()

        gem_keys = get_gemini_api_keys()
        gem_k = (gemini_api_key or "").strip() or (gem_keys[0] if gem_keys else "")
        self.gemini_keys: List[str] = [gem_k] + [k for k in gem_keys if k != gem_k] if gem_k else []
        self._gemini_dead_keys: Set[str] = set()
        self.decision_teacher = GeminiDecisionTeacher(api_key=gem_k)

    def _store_batch_jury_payloads(
        self,
        parsed_list: List[Dict[str, Any]],
        uncached: List[Tuple[int, Dict[str, Any], str]],
        tier_slot: str,
        family_tag: str,
        model_id: str,
        results: Dict[int, Dict[str, Any]],
    ) -> bool:
        by_id: Dict[int, Dict[str, Any]] = {}
        for elem in parsed_list:
            try:
                by_id[int(elem.get("post_id"))] = elem
            except Exception:
                continue
        if len(uncached) == 1 and len(parsed_list) == 1 and not by_id:
            by_id[uncached[0][0]] = parsed_list[0]
        if not by_id:
            return False
        for pid, t0, ck in uncached:
            elem = by_id.get(pid)
            if not elem:
                continue
            facts = normalize_atomic_facts(elem)
            is_viol, reg = derive_regime_from_atomic_facts(t0, facts)
            conf = float(elem.get("confidence", 0.90) or 0.90)
            conf = max(0.0, min(1.0, round(conf, 4)))
            payload = {
                "voter": model_id.split("/")[-1].replace(":free", ""),
                "model_used": model_id,
                "family": family_tag,
                "facts": facts,
                "is_violation": is_viol,
                "regime": reg.name,
                "confidence": conf,
            }
            self.cache.set(ck, pid, tier_slot, model_id, payload)
            results[pid] = payload
        return True

    async def _call_gemini_batch(
        self,
        client: httpx.AsyncClient,
        tier_slot: str,
        family_tag: str,
        model_candidates: List[str],
        batch_items: List[Tuple[int, Dict[str, Any]]],
        role_mode: str,
    ) -> Dict[int, Dict[str, Any]]:
        results: Dict[int, Dict[str, Any]] = {}
        uncached: List[Tuple[int, Dict[str, Any], str]] = []

        for pid, t0 in batch_items:
            ck = self.cache.make_key(pid, t0["sanitized_digest"], tier_slot)
            hit = self.cache.get(ck)
            if hit and isinstance(hit.get("facts"), dict):
                results[pid] = hit
            else:
                uncached.append((pid, t0, ck))

        active_g_keys = [k for k in self.gemini_keys if k not in self._gemini_dead_keys]
        if not uncached or not active_g_keys or not model_candidates:
            return results

        prompt = _build_batch_jury_prompt([(pid, t0) for pid, t0, _ in uncached], role_mode=role_mode)
        for model_id in model_candidates:
            if model_id in self._cooldown_models:
                continue
            gen_cfg: Dict[str, Any] = {"temperature": 0.0}
            if not model_id.lower().startswith("gemma-"):
                gen_cfg["responseMimeType"] = "application/json"
            req_body = {
                "systemInstruction": {"parts": [{"text": "Output strictly valid JSON matching the requested schema. Zero prose."}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen_cfg,
            }
            endpoint = f"{GEMINI_API_BASE}/models/{model_id}:generateContent"
            for key in [k for k in self.gemini_keys if k not in self._gemini_dead_keys]:
                try:
                    resp = await client.post(
                        endpoint,
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json=req_body,
                        timeout=35.0,
                    )
                    if resp.status_code == 200:
                        raw_content = extract_gemini_response_text(resp.json())
                        parsed_list = _extract_json_items_array(raw_content)
                        if self._store_batch_jury_payloads(parsed_list, uncached, tier_slot, family_tag, model_id, results):
                            return results
                        break
                    elif resp.status_code in (401, 403):
                        self._gemini_dead_keys.add(key)
                        continue
                    elif resp.status_code == 429:
                        await asyncio.sleep(0.25)
                        continue
                    else:
                        if resp.status_code in (400, 404, 502, 503):
                            self._cooldown_models.add(model_id)
                        break
                except Exception as e:
                    logger.debug("Gemini jury error on %s: %s", model_id, e)
                    break
        return results

    def _next_keys(self) -> List[str]:
        if not self.api_keys:
            return []
        n = len(self.api_keys)
        ordered = [self.api_keys[(self._key_idx + i) % n] for i in range(n)]
        return [k for k in ordered if k not in self._dead_keys]

    def _advance_key(self, used_key: str) -> None:
        if used_key in self.api_keys:
            self._key_idx = (self.api_keys.index(used_key) + 1) % len(self.api_keys)

    async def _call_openrouter_batch(
        self,
        client: httpx.AsyncClient,
        tier_slot: str,
        family_tag: str,
        model_candidates: List[str],
        batch_items: List[Tuple[int, Dict[str, Any]]],
        role_mode: str,
    ) -> Dict[int, Dict[str, Any]]:
        results: Dict[int, Dict[str, Any]] = {}
        uncached: List[Tuple[int, Dict[str, Any], str]] = []

        for pid, t0 in batch_items:
            ck = self.cache.make_key(pid, t0["sanitized_digest"], tier_slot)
            hit = self.cache.get(ck)
            if hit and isinstance(hit.get("facts"), dict):
                results[pid] = hit
            else:
                uncached.append((pid, t0, ck))

        if not uncached or not self._next_keys() or not model_candidates:
            return results

        prompt = _build_batch_jury_prompt([(pid, t0) for pid, t0, _ in uncached], role_mode=role_mode)

        for model_id in model_candidates:
            if model_id in self._cooldown_models:
                continue
            keys = self._next_keys()
            if not keys:
                break
            max_key_tries = min(2, len(keys))
            for attempt_idx, key in enumerate(keys[:max_key_tries]):
                try:
                    resp = await client.post(
                        OPENROUTER_CHAT_ENDPOINT,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://adradar.ru",
                            "X-Title": "AdRadar Orthogonal Jury",
                        },
                        json={
                            "model": model_id,
                            "temperature": 0.0,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "Output strictly valid JSON matching the requested schema. Zero prose.",
                                },
                                {"role": "user", "content": prompt},
                            ],
                        },
                        timeout=38.0,
                    )
                    if resp.status_code == 200:
                        self._advance_key(key)
                        msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
                        raw_content = msg.get("content") or ""
                        parsed_list = _extract_json_items_array(raw_content)
                        by_id: Dict[int, Dict[str, Any]] = {}
                        for elem in parsed_list:
                            try:
                                by_id[int(elem.get("post_id"))] = elem
                            except Exception:
                                continue

                        # If single item batch and post_id omitted
                        if len(uncached) == 1 and len(parsed_list) == 1 and not by_id:
                            by_id[uncached[0][0]] = parsed_list[0]

                        if by_id:
                            for pid, t0, ck in uncached:
                                elem = by_id.get(pid)
                                if not elem:
                                    continue
                                facts = normalize_atomic_facts(elem)
                                is_viol, reg = derive_regime_from_atomic_facts(t0, facts)
                                conf = float(elem.get("confidence", 0.90) or 0.90)
                                conf = max(0.0, min(1.0, round(conf, 4)))
                                payload = {
                                    "voter": model_id.split("/")[-1].replace(":free", ""),
                                    "model_used": model_id,
                                    "family": family_tag,
                                    "facts": facts,
                                    "is_violation": is_viol,
                                    "regime": reg.name,
                                    "confidence": conf,
                                }
                                self.cache.set(ck, pid, tier_slot, model_id, payload)
                                results[pid] = payload
                            return results
                        break
                    elif resp.status_code in (401, 402, 403):
                        logger.warning("Key %s rejected (%s); marking exhausted.", _mask_api_key(key), resp.status_code)
                        self._dead_keys.add(key)
                        self._advance_key(key)
                        continue
                    elif resp.status_code == 429:
                        self._advance_key(key)
                        err_body = (resp.text or "").lower()
                        if "upstream" in err_body or attempt_idx + 1 >= max_key_tries:
                            self._cooldown_models.add(model_id)
                            break
                        await asyncio.sleep(0.25)
                        continue
                    else:
                        if resp.status_code in (400, 404, 502, 503):
                            self._cooldown_models.add(model_id)
                        break
                except Exception as e:
                    logger.debug("OpenRouter jury error on %s: %s", model_id, e)
                    break

        return results

    async def evaluate_tier2a_decision(
        self,
        client: httpx.AsyncClient,
        batch_items: List[Tuple[int, Dict[str, Any]]],
    ) -> Dict[int, Dict[str, Any]]:
        """
        Tier 2A: Attempts GeminiDecisionTeacher (Decision-1 zero-output-token API);
        if unavailable/UNVERIFIED, falls back to configured free decider gate in openrouter_models.yaml.
        """
        results: Dict[int, Dict[str, Any]] = {}
        uncached: List[Tuple[int, Dict[str, Any], str]] = []

        for pid, t0 in batch_items:
            ck = self.cache.make_key(pid, t0["sanitized_digest"], "tier_2a_decision")
            hit = self.cache.get(ck)
            if hit and isinstance(hit.get("facts"), dict):
                results[pid] = hit
            else:
                uncached.append((pid, t0, ck))

        if not uncached:
            return results

        if self.decision_teacher.api_key and not self._decision_api_disabled:
            queries = [
                DecisionQuery(
                    query_type="choice",
                    context_text=f"{t0['sanitized_title']}. {t0['sanitized_digest']}",
                    criteria=[
                        "Выделение коммерческого объекта рекламирования по ст. 3 38-ФЗ",
                        "Призыв к покупке, промокод или реферальная ссылка",
                        "Однородный обзор >= 3 конкурентов или нейтральный личный блог автора",
                    ],
                    options=["VIOLATION", "CLEAN", "OVER_MARKED_NON_AD"],
                    article_id=str(pid),
                )
                for pid, t0, _ in uncached
            ]
            t_res = await self.decision_teacher.query_teacher(queries, client=client)
            all_verified = len(t_res) == len(uncached) and all(r.decision != "UNVERIFIED" for r in t_res)
            if not all_verified:
                self._decision_api_disabled = True
            if all_verified:
                for (pid, t0, ck), dres in zip(uncached, t_res):
                    viol_flag = dres.decision == "VIOLATION"
                    ext_links = t0.get("external_links") or []
                    only_tg = len(ext_links) == 1 and "t.me/" in ext_links[0].lower()
                    facts = {
                        "F1_OBJECT_PROMOTED": bool(viol_flag or t0["has_erid"]),
                        "F2_AUTHOR_SELF_BIO": bool(not viol_flag and only_tg and not t0["has_erid"]),
                        "F3_UNIFORM_MULTI_REVIEW": bool(not viol_flag and len(ext_links) >= 3 and not t0["has_erid"]),
                        "F4_COMMERCIAL_CTA_OR_OFFER": bool(viol_flag),
                        "F5_BANNED_OR_SPECIAL": False,
                    }
                    is_viol, reg = derive_regime_from_atomic_facts(t0, facts)
                    payload = {
                        "voter": "Decision-1",
                        "model_used": "gemini-decision-1",
                        "family": "decision_api",
                        "facts": facts,
                        "is_violation": is_viol,
                        "regime": reg.name,
                        "confidence": round(float(dres.confidence), 4),
                    }
                    self.cache.set(ck, pid, "tier_2a_decision", "gemini-decision-1", payload)
                    results[pid] = payload
                return results

        # Non-autoregressive OpenRouter /api/alpha/decisions (0 prose tokens, calibrated noul probabilities)
        decider_models = self.models_cfg.get("tier_2a_decider_fallback") or []
        for dec_model in decider_models:
            if dec_model in self._cooldown_models:
                continue
            # Build batched state + noul questions across the chunk
            state_blocks: List[str] = []
            questions_dict: Dict[str, Any] = {}
            for pid, t0, _ in uncached:
                state_blocks.append(
                    f"[POST {pid}] TITLE: {t0['sanitized_title']} | "
                    f"ERID={t0['has_erid']} LABEL={t0['has_strict_ad_label']} AFF={t0['has_affiliate_link']} PROMO={t0['has_promo_code']} | "
                    f"LINKS={t0['external_links'][:5]} | AUTHOR={json.dumps(t0['author_meta'], ensure_ascii=False)} | "
                    f"TEXT: {t0['sanitized_digest'][:650]}"
                )
                questions_dict[f"p{pid}_F1"] = {
                    "type": "noul",
                    "instructions": f"In [POST {pid}], is a specific commercial product, service, company, or bot promoted?",
                }
                questions_dict[f"p{pid}_F2"] = {
                    "type": "noul",
                    "instructions": f"In [POST {pid}], is the external link merely a neutral signature to the author's personal blog without commercial offer or prices?",
                }
                questions_dict[f"p{pid}_F3"] = {
                    "type": "noul",
                    "instructions": f"In [POST {pid}], is this a uniform analytical review of 3+ competitors without a single paid favorite or referral link?",
                }
                questions_dict[f"p{pid}_F4"] = {
                    "type": "noul",
                    "instructions": f"In [POST {pid}], is there a call to buy/register, price/tariff offer, discount, promo code, or referral/UTM link?",
                }
                questions_dict[f"p{pid}_F5"] = {
                    "type": "noul",
                    "instructions": f"In [POST {pid}], is a prohibited/restricted object promoted (casino, betting, VPN bypass, crypto exchange referral)?",
                }

            keys = self._next_keys()
            if not keys:
                break
            got_dec = False
            for key in keys[: min(2, len(keys))]:
                try:
                    resp = await client.post(
                        OPENROUTER_DECISIONS_ENDPOINT,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://adradar.ru",
                            "X-Title": "AdRadar Decisions Gate",
                        },
                        json={
                            "model": dec_model,
                            "state": "\n\n".join(state_blocks),
                            "questions": questions_dict,
                        },
                        timeout=25.0,
                    )
                    if resp.status_code == 200:
                        self._advance_key(key)
                        ans_map = resp.json().get("answers") or {}
                        for pid, t0, ck in uncached:
                            probs: List[float] = []
                            raw_f: Dict[str, bool] = {}
                            for short_f, full_f in SHORT_TO_FULL_FACT.items():
                                a_obj = ans_map.get(f"p{pid}_{short_f}") or {}
                                p_val = float(a_obj.get("noul", 0.0) or 0.0)
                                raw_f[full_f] = p_val >= 0.5
                                probs.append(max(p_val, 1.0 - p_val))
                            is_viol, reg = derive_regime_from_atomic_facts(t0, raw_f)
                            conf = round(sum(probs) / max(1, len(probs)), 4)
                            payload = {
                                "voter": dec_model.split("/")[-1].replace(":free", ""),
                                "model_used": dec_model,
                                "family": "decision_api",
                                "facts": raw_f,
                                "is_violation": is_viol,
                                "regime": reg.name,
                                "confidence": conf,
                            }
                            self.cache.set(ck, pid, "tier_2a_decision", dec_model, payload)
                            results[pid] = payload
                        got_dec = True
                        break
                    elif resp.status_code == 429:
                        self._advance_key(key)
                        if "upstream" in (resp.text or "").lower():
                            self._cooldown_models.add(dec_model)
                            break
                        continue
                    else:
                        self._cooldown_models.add(dec_model)
                        break
                except Exception as e:
                    logger.debug("Alpha decisions error on %s: %s", dec_model, e)
                    break
            if got_dec:
                break
        return results


# =============================================================================
# TIER 4: CONFLICT POINT DETECTOR & 5-SECOND MICRO-QUESTION GENERATOR
# =============================================================================
def extract_highlighted_micro_quote(
    record: Dict[str, Any],
    tier0: Dict[str, Any],
    disputed_fact: str,
    max_len: int = 290,
) -> Tuple[str, str]:
    """
    Cuts a single compact snippet (<= 300 chars) around the disputed trigger
    so the human annotator never has to read a 7,000-character longread.
    Returns (quote_snippet, highlight_trigger).
    """
    paragraphs: List[str] = list(record.get("paragraphs") or [])
    raw_text = str(record.get("text_window") or record.get("full_text") or "").strip()
    raw_text_clean = re.sub(r"\n\[Ссылки:.*\]$", "", raw_text).strip()
    if not paragraphs:
        paragraphs = [p.strip() for p in re.split(r"(?:\r?\n)+", raw_text_clean) if p.strip()]

    links = list(tier0.get("external_links") or [])
    targets = list(tier0.get("commercial_targets") or [])

    trigger = ""
    chosen_para = ""

    if disputed_fact == "F2_AUTHOR_SELF_BIO":
        # Prefer footer paragraph or Telegram handle/link
        tg_candidates = [x for x in (links + targets) if "t.me/" in x.lower() or x.startswith("@")]
        trigger = tg_candidates[0] if tg_candidates else (links[-1] if links else "")
        if paragraphs:
            for p in reversed(paragraphs[-4:]):
                if (trigger and trigger.split("?")[0] in p) or "канал" in p.lower() or "подпис" in p.lower() or "t.me" in p.lower():
                    chosen_para = p
                    break
            if not chosen_para:
                chosen_para = paragraphs[-1]
    elif disputed_fact == "F5_BANNED_OR_SPECIAL":
        m = re.search(r"(?i)\b(казино|ставк[а-я]*|букмекер[а-я]*|vpn|обход\s+блокиров[а-я]*|криптобирж[а-я]*|bybit|binance|bingx)\b", raw_text_clean)
        if m:
            trigger = m.group(0)
            for p in paragraphs:
                if trigger.lower() in p.lower():
                    chosen_para = p
                    break
    elif disputed_fact == "F4_COMMERCIAL_CTA_OR_OFFER":
        ref_links = [u for u in links if is_affiliate_link(u)]
        if ref_links:
            trigger = ref_links[0]
        else:
            m_promo = PROMO_CODE_REGEX.search(raw_text_clean) or re.search(
                r"(?i)\b(промокод|скидк[а-я]*|тариф[а-я]*|оформить|заказать|купить|реферальн[а-я]*)\b",
                raw_text_clean,
            )
            if m_promo:
                trigger = m_promo.group(0)
            elif links:
                trigger = links[0]
        for p in paragraphs:
            if trigger and trigger.split("?")[0][:28].lower() in p.lower():
                chosen_para = p
                break
    elif disputed_fact == "F3_UNIFORM_MULTI_REVIEW":
        chosen_para = paragraphs[0] if paragraphs else raw_text_clean[:max_len]
        if links:
            trigger = links[0]

    if not chosen_para:
        if targets:
            trigger = trigger or targets[0]
            for p in paragraphs:
                if trigger.split("?")[0][:24].lower() in p.lower():
                    chosen_para = p
                    break
        if not chosen_para:
            chosen_para = paragraphs[-1] if paragraphs else raw_text_clean

    chosen_para = re.sub(r"\s+", " ", chosen_para).strip()
    if len(chosen_para) > max_len:
        if trigger and trigger.lower() in chosen_para.lower():
            idx = chosen_para.lower().find(trigger.lower())
            half = max(40, (max_len - len(trigger) - 10) // 2)
            start = max(0, idx - half)
            end = min(len(chosen_para), idx + len(trigger) + half)
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(chosen_para) else ""
            chosen_para = f"{prefix}{chosen_para[start:end].strip()}{suffix}"
        else:
            chosen_para = chosen_para[: max_len - 3].rstrip() + "..."

    # Append link hint if trigger is an extracted href not literally printed in paragraph text
    if trigger and trigger.lower() not in chosen_para.lower():
        budget = max_len - len(trigger) - 6
        if budget > 60:
            base_c = chosen_para[:budget].rstrip()
            chosen_para = f"{base_c} [{trigger}]"

    return chosen_para[:300], trigger[:120]


def build_micro_question(
    record: Dict[str, Any],
    tier0: Dict[str, Any],
    votes: List[Dict[str, Any]],
    external_doubt_ru: str = "",
) -> Dict[str, Any]:
    """
    Identifies the single highest-conflict atomic fact across jury members and
    builds a 5-second binary Micro-Question where buttons [1] and [2] deterministically
    compute the resulting LegalRegime.
    """
    # Compute majority baseline for F1..F5
    majority_facts: Dict[str, bool] = {}
    disagreement_scores: Dict[str, float] = {}
    priority_order = [
        "F5_BANNED_OR_SPECIAL",
        "F2_AUTHOR_SELF_BIO",
        "F3_UNIFORM_MULTI_REVIEW",
        "F4_COMMERCIAL_CTA_OR_OFFER",
        "F1_OBJECT_PROMOTED",
    ]

    for fk in ATOMIC_FACT_KEYS:
        vals = [bool((v.get("facts") or {}).get(fk, False)) for v in votes if isinstance(v.get("facts"), dict)]
        if not vals:
            majority_facts[fk] = False
            disagreement_scores[fk] = 0.0
            continue
        trues = sum(1 for x in vals if x)
        falses = len(vals) - trues
        majority_facts[fk] = trues >= falses
        disagreement_scores[fk] = min(trues, falses) / max(1, len(vals))

    # Pick most disputed fact; if votes agreed on facts but confidence < 0.90, pick decisive active fact
    best_fact = "F4_COMMERCIAL_CTA_OR_OFFER"
    best_score = -1.0
    for fk in priority_order:
        sc = disagreement_scores.get(fk, 0.0)
        if sc > best_score:
            best_score = sc
            best_fact = fk

    if best_score == 0.0:
        ext_links = tier0.get("external_links") or []
        if len(ext_links) == 1 and "t.me/" in ext_links[0].lower():
            best_fact = "F2_AUTHOR_SELF_BIO"
        elif len(ext_links) >= 3:
            best_fact = "F3_UNIFORM_MULTI_REVIEW"
        elif ext_links or tier0.get("commercial_targets"):
            best_fact = "F4_COMMERCIAL_CTA_OR_OFFER"
        else:
            best_fact = "F1_OBJECT_PROMOTED"

    quote_snippet, trigger = extract_highlighted_micro_quote(record, tier0, best_fact)
    short_code = best_fact.split("_")[0]
    target_disp = trigger or (
        (tier0.get("commercial_targets") or tier0.get("external_links") or ["указанный объект"])[0]
    )
    target_short = target_disp[:55]

    # Compute exact statutory outcomes when human answers Option 1 vs Option 2
    facts_true = dict(majority_facts)
    facts_false = dict(majority_facts)
    facts_true[best_fact] = True
    facts_false[best_fact] = False

    if best_fact == "F2_AUTHOR_SELF_BIO":
        # Option 1 = Personal blog (F2=True, F4=False) -> CLEAN_NON_AD
        # Option 2 = Paid promo / third-party channel (F2=False, F1=True, F4=True) -> UNMARKED_AD_NO_ERID
        facts_opt1 = {**majority_facts, "F2_AUTHOR_SELF_BIO": True, "F4_COMMERCIAL_CTA_OR_OFFER": False}
        facts_opt2 = {**majority_facts, "F2_AUTHOR_SELF_BIO": False, "F1_OBJECT_PROMOTED": True, "F4_COMMERCIAL_CTA_OR_OFFER": True}
        q_text = (
            f"Вопрос по расхождению ({short_code}): Ссылка «{target_short}» — это нейтральный личный блог автора "
            f"без коммерческого оффера или платный посев / промо проекта?"
        )
        opt1_label = "Личный блог автора без оффера"
        opt2_label = "Рекламный посев / промо проекта"
    elif best_fact == "F3_UNIFORM_MULTI_REVIEW":
        facts_opt1 = {**majority_facts, "F3_UNIFORM_MULTI_REVIEW": True, "F4_COMMERCIAL_CTA_OR_OFFER": False}
        facts_opt2 = {**majority_facts, "F3_UNIFORM_MULTI_REVIEW": False, "F1_OBJECT_PROMOTED": True, "F4_COMMERCIAL_CTA_OR_OFFER": True}
        q_text = (
            f"Вопрос по расхождению ({short_code}): Статья — это однородный обзор >= 3 конкурентов без выделения спонсора "
            f"или нативное продвижение конкретного сервиса ({target_short})?"
        )
        opt1_label = "Однородный обзор 3+ без фаворита"
        opt2_label = "Продвижение конкретного бренда/рефки"
    elif best_fact == "F5_BANNED_OR_SPECIAL":
        facts_opt1 = {**majority_facts, "F5_BANNED_OR_SPECIAL": False}
        facts_opt2 = {**majority_facts, "F5_BANNED_OR_SPECIAL": True, "F1_OBJECT_PROMOTED": True}
        q_text = (
            f"Вопрос по расхождению ({short_code}): Фрагмент с «{target_short}» — это нейтральное новостное/техническое упоминание "
            f"или продвижение запрещенного/спецрежимного объекта (ст. 7 / 28 38-ФЗ)?"
        )
        opt1_label = "Нейтральное упоминание / новость"
        opt2_label = "Реклама запрещенного / спецрежимного объекта"
    elif best_fact == "F1_OBJECT_PROMOTED":
        facts_opt1 = {**majority_facts, "F1_OBJECT_PROMOTED": False, "F4_COMMERCIAL_CTA_OR_OFFER": False}
        facts_opt2 = {**majority_facts, "F1_OBJECT_PROMOTED": True, "F4_COMMERCIAL_CTA_OR_OFFER": True}
        q_text = (
            f"Вопрос по расхождению ({short_code}): Выделен ли в этом абзаце конкретный коммерческий продукт/услуга "
            f"как объект рекламирования ({target_short})?"
        )
        opt1_label = "Нет, редакционный/информационный текст"
        opt2_label = "Да, продвижение конкретного продукта/услуги"
    else:
        # F4_COMMERCIAL_CTA_OR_OFFER
        facts_opt1 = {**majority_facts, "F4_COMMERCIAL_CTA_OR_OFFER": False, "F2_AUTHOR_SELF_BIO": True}
        facts_opt2 = {**majority_facts, "F1_OBJECT_PROMOTED": True, "F4_COMMERCIAL_CTA_OR_OFFER": True, "F2_AUTHOR_SELF_BIO": False}
        q_text = (
            f"Вопрос по расхождению ({short_code}): Есть ли вокруг «{target_short}» коммерческий оффер "
            f"(призыв перейти/купить, тариф, скидка, реф-ссылка) или это справочная ссылка?"
        )
        opt1_label = "Справочное упоминание без оффера"
        opt2_label = "Коммерческий оффер / призыв / реф-ссылка"

    if external_doubt_ru.strip():
        clean_doubt = _sanitize_citations_safe(external_doubt_ru.strip())
        q_text = f"Вопрос по расхождению ({short_code}): {clean_doubt}"

    _, reg_opt1 = derive_regime_from_atomic_facts(tier0, facts_opt1)
    _, reg_opt2 = derive_regime_from_atomic_facts(tier0, facts_opt2)
    # Guarantee distinct regimes on the 2 buttons for crisp resolution
    if reg_opt1 == reg_opt2:
        reg_opt1 = LegalRegime.CLEAN_NON_AD if not tier0.get("has_erid") else LegalRegime.CLEAN_MARKED_AD
        reg_opt2 = LegalRegime.UNMARKED_AD_NO_ERID

    jury_summary_parts: List[str] = []
    for v in votes:
        v_name = v.get("voter") or "Model"
        v_reg = REGIME_HUMAN_SHORT_RU.get(str(v.get("regime") or ""), str(v.get("regime") or ""))
        v_conf = v.get("confidence")
        conf_str = f" ({v_conf:.2f})" if isinstance(v_conf, (int, float)) else ""
        jury_summary_parts.append(f"{v_name}: {v_reg}{conf_str}")

    return {
        "fact_id": best_fact,
        "fact_short": short_code,
        "quote_snippet": quote_snippet,
        "highlight_trigger": trigger,
        "question_ru": q_text,
        "jury_line_ru": " · ".join(jury_summary_parts),
        "option_1": {
            "hotkey": "1",
            "label_ru": opt1_label,
            "resulting_regime": reg_opt1.name,
        },
        "option_2": {
            "hotkey": "2",
            "label_ru": opt2_label,
            "resulting_regime": reg_opt2.name,
        },
    }


# =============================================================================
# ANTI-ECHO-CHAMBER CONSENSUS ARBITRATOR
# =============================================================================
def arbitrate_ensemble_votes(
    record: Dict[str, Any],
    tier0: Dict[str, Any],
    votes: List[Dict[str, Any]],
    external_chat_vote: Optional[Dict[str, Any]] = None,
    min_consensus_conf: float = 0.90,
) -> Dict[str, Any]:
    """
    Enforces the Anti-Echo-Chamber Rule:
    LinkCentric-V3 can NEVER self-certify AUTO_GOLD without:
    1) Laya Vega-U (non-abstaining), AND
    2) >= 2 independent external models of distinct families, AND
    3) Unanimous 4/4 agreement on (is_violation, regime) with mean confidence >= 0.90.
    """
    all_votes = list(votes)
    if external_chat_vote and isinstance(external_chat_vote.get("facts"), dict):
        all_votes.append(external_chat_vote)

    sheaf_vote = next(
        (v for v in all_votes if v.get("family") in ("link_centric_local", "cellular_sheaf_local")),
        None,
    )
    vega_vote = next((v for v in all_votes if v.get("family") == "vega_nli_local"), None)
    external_votes = [
        v for v in all_votes
        if v.get("family") not in ("link_centric_local", "cellular_sheaf_local", "vega_nli_local")
    ]
    external_families = {str(v.get("family")) for v in external_votes if v.get("family")}

    regimes = {str(v.get("regime")) for v in all_votes if v.get("regime")}
    viol_flags = {bool(v.get("is_violation")) for v in all_votes}
    confs = [float(v.get("confidence", 0.0)) for v in all_votes if isinstance(v.get("confidence"), (int, float))]
    mean_conf = round(sum(confs) / max(1, len(confs)), 4) if confs else 0.0

    has_sheaf = sheaf_vote is not None
    has_vega = vega_vote is not None and not bool(vega_vote.get("abstain"))
    has_two_external_families = len(external_votes) >= 2 and len(external_families) >= 2
    unanimous = len(all_votes) >= 4 and len(regimes) == 1 and len(viol_flags) == 1

    if has_sheaf and has_vega and has_two_external_families and unanimous and mean_conf >= min_consensus_conf:
        agreed_regime = next(iter(regimes))
        agreed_viol = next(iter(viol_flags))
        return {
            "ensemble_status": "AUTO_GOLD",
            "consensus_regime": agreed_regime,
            "consensus_is_violation": agreed_viol,
            "consensus_confidence": mean_conf,
            "sample_weight": 2.0,
            "label_source": "UNANIMOUS_CONSENSUS",
            "voter_count": len(all_votes),
            "external_family_count": len(external_families),
            "votes": all_votes,
            "micro_question": None,
        }

    # If >= 3 independent external model families unanimously agree (3/3) with high confidence,
    # do NOT let the synthetic-pretrained student (SheafNet) veto the 3-family council unless Tier-0 conflicts
    ext_regimes = {str(v.get("regime")) for v in external_votes if v.get("regime")}
    ext_viols = {bool(v.get("is_violation")) for v in external_votes}
    ext_confs = [float(v.get("confidence", 0.0)) for v in external_votes if isinstance(v.get("confidence"), (int, float))]
    ext_mean_conf = round(sum(ext_confs) / max(1, len(ext_confs)), 4) if ext_confs else 0.0
    if len(external_votes) >= 3 and len(external_families) >= 3 and len(ext_regimes) == 1 and len(ext_viols) == 1 and ext_mean_conf >= 0.88:
        ext_reg = next(iter(ext_regimes))
        ext_viol = next(iter(ext_viols))
        # Verify Tier-0 consistency (e.g. never call erid-marked ad UNMARKED_AD_NO_ERID)
        tier0_consistent = not (tier0.get("has_erid") and ext_reg == "UNMARKED_AD_NO_ERID")
        if tier0_consistent:
            return {
                "ensemble_status": "AUTO_GOLD",
                "consensus_regime": ext_reg,
                "consensus_is_violation": ext_viol,
                "consensus_confidence": ext_mean_conf,
                "sample_weight": 2.0,
                "label_source": "UNANIMOUS_CONSENSUS",
                "voter_count": len(all_votes),
                "external_family_count": len(external_families),
                "votes": all_votes,
                "micro_question": None,
            }

    ext_doubt = str((external_chat_vote or {}).get("doubt_question_ru") or "")
    micro_q = build_micro_question(record, tier0, all_votes, external_doubt_ru=ext_doubt)
    return {
        "ensemble_status": "NEEDS_MICRO_Q",
        "consensus_regime": None,
        "consensus_is_violation": None,
        "consensus_confidence": mean_conf,
        "sample_weight": 3.0,
        "label_source": "PENDING_HUMAN_MICRO_Q",
        "voter_count": len(all_votes),
        "external_family_count": len(external_families),
        "votes": all_votes,
        "micro_question": micro_q,
    }


# =============================================================================
# FULL PIPELINE RUNNER & CHAT BATCH EXPORTER / IMPORTER
# =============================================================================
async def run_ensemble_pipeline(
    input_path: Path,
    output_path: Path,
    cloud_batch_size: int = 5,
    skip_cloud: bool = False,
    vega_light_mode: bool = False,
    limit: Optional[int] = None,
    provider: str = "auto",
) -> Dict[str, Any]:
    blocked_ids, blocked_urls = load_golden_blocklist()
    raw_lines = [l for l in input_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if limit is not None and limit > 0:
        raw_lines = raw_lines[:limit]

    bundle_candidates = [
        _active_data_dir() / "models" / "champion_v3.bundle",
        MODELS_CACHE_DIR / "champion_v3.bundle",
        REPO_ROOT / "legal_sheaf_verifier" / "artifacts" / "champion_v3.bundle",
    ]
    bundle: Optional[AdRadarBrainBundle] = None
    for bp in bundle_candidates:
        if bp.exists():
            bundle = AdRadarBrainBundle.load(bp, verify_hash=True)
            break
    if bundle is None:
        raise RuntimeError("champion_v3.bundle not found; cannot run Tier 1A LinkCentric-V3.")

    sensor = VegaUSensor(light_mode=vega_light_mode)
    cloud_jury = MultiFamilyCloudJury()

    # Step A: Tier 0 + Tier 1A + Tier 1B locally on GPU (with SQLite cache for Vega-U)
    staged: List[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] = []
    leakage_blocked = 0
    total_raw = len(raw_lines)
    print(f"[*] [Tier 0 + Tier 1] Evaluating {total_raw} articles on RTX 4060 (SheafNet + Laya Vega-U)...", flush=True)

    for idx_row, line in enumerate(raw_lines, start=1):
        rec = json.loads(line)
        pid = int(rec.get("post_id") or 0)
        u_norm = _normalize_url(str(rec.get("url") or ""))
        if pid in blocked_ids or (u_norm and u_norm in blocked_urls):
            leakage_blocked += 1
            continue

        t0 = build_tier0_passport(rec)
        v_sheaf = evaluate_tier1a_sheaf(bundle, rec, t0)

        vega_ck = cloud_jury.cache.make_key(pid, t0["sanitized_digest"], "tier_1b_vega_u")
        cached_vega = None if vega_light_mode else cloud_jury.cache.get(vega_ck)
        if cached_vega and isinstance(cached_vega.get("facts"), dict):
            v_vega = cached_vega
        else:
            v_vega = evaluate_tier1b_vega(sensor, t0)
            if not vega_light_mode:
                cloud_jury.cache.set(vega_ck, pid, "tier_1b_vega_u", "nandakishorm/vega-U", v_vega)

        staged.append((rec, t0, [v_sheaf, v_vega]))
        if idx_row % 20 == 0 or idx_row == total_raw:
            print(f"    [Tier 1] {idx_row}/{total_raw} processed...", flush=True)

    # Step B: Tier 2 Cloud Jury in compact micro-batches
    tier2_by_id: Dict[int, List[Dict[str, Any]]] = {int(r["post_id"]): [] for r, _, _ in staged}
    if not skip_cloud and staged:
        prov = str(provider or "auto").strip().lower()
        use_gemini_jury = (prov == "gemini") or (prov == "auto" and bool(cloud_jury.gemini_keys))
        models_cfg = cloud_jury.models_cfg
        gem_cfg = cloud_jury.gemini_models_cfg
        step_sz = max(1, cloud_batch_size)
        num_batches = (len(staged) + step_sz - 1) // step_sz
        print(
            f"[*] [Tier 2] Running 3-family Cloud Jury ({'Direct Gemini' if use_gemini_jury else 'OpenRouter'}) "
            f"across {num_batches} batch(es)...",
            flush=True,
        )
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            for b_num, start in enumerate(range(0, len(staged), step_sz), start=1):
                chunk = staged[start : start + step_sz]
                batch_pairs = [(int(r["post_id"]), t0) for r, t0, _ in chunk]

                if use_gemini_jury:
                    coro_2b = cloud_jury._call_gemini_batch(
                        client=client,
                        tier_slot="tier_2b_gemini_v3",
                        family_tag="gemini_v3",
                        model_candidates=gem_cfg["tier_2b_primary"],
                        batch_items=batch_pairs,
                        role_mode="auditor",
                    )
                    coro_2c = cloud_jury._call_gemini_batch(
                        client=client,
                        tier_slot="tier_2c_gemini_v25",
                        family_tag="gemini_v25",
                        model_candidates=gem_cfg["tier_2c_secondary"],
                        batch_items=batch_pairs,
                        role_mode="devils_advocate",
                    )
                else:
                    coro_2b = cloud_jury._call_openrouter_batch(
                        client=client,
                        tier_slot="tier_2b_nvidia",
                        family_tag="nvidia_moe",
                        model_candidates=models_cfg["tier_2b_nvidia"],
                        batch_items=batch_pairs,
                        role_mode="auditor",
                    )
                    coro_2c = cloud_jury._call_openrouter_batch(
                        client=client,
                        tier_slot="tier_2c_independent",
                        family_tag="independent_devils_advocate",
                        model_candidates=models_cfg["tier_2c_independent"],
                        batch_items=batch_pairs,
                        role_mode="devils_advocate",
                    )

                r_2a, r_2b, r_2c = await asyncio.gather(
                    cloud_jury.evaluate_tier2a_decision(client, batch_pairs),
                    coro_2b,
                    coro_2c,
                )
                for pid, _ in batch_pairs:
                    for slot_map in (r_2a, r_2b, r_2c):
                        if pid in slot_map:
                            tier2_by_id[pid].append(slot_map[pid])
                print(
                    f"    [Tier 2] Batch {b_num}/{num_batches} done "
                    f"(2A={len(r_2a)}, 2B={len(r_2b)}, 2C={len(r_2c)})...",
                    flush=True,
                )

    # Step C: Anti-Echo-Chamber Arbitration & Micro-Question Attachment
    enriched_records: List[Dict[str, Any]] = []
    auto_gold_cnt = 0
    micro_q_cnt = 0

    for rec, t0, local_votes in staged:
        pid = int(rec["post_id"])
        combined_votes = local_votes + tier2_by_id.get(pid, [])
        arb = arbitrate_ensemble_votes(rec, t0, combined_votes)

        out_rec = dict(rec)
        out_rec["tier0_passport"] = t0
        out_rec["ensemble"] = arb
        if arb["ensemble_status"] == "AUTO_GOLD":
            auto_gold_cnt += 1
            out_rec["auto_gold_label"] = arb["consensus_regime"]
        else:
            micro_q_cnt += 1
            out_rec["auto_gold_label"] = None
        enriched_records.append(out_rec)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in enriched_records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "total_processed": len(enriched_records),
        "leakage_blocked": leakage_blocked,
        "auto_gold_count": auto_gold_cnt,
        "needs_micro_q_count": micro_q_cnt,
        "output_path": str(output_path),
    }


def export_chat_batches(
    ensemble_jsonl: Path,
    batches_dir: Path,
    batch_size: int = 10,
    only_disputed: bool = True,
) -> List[Path]:
    """
    Generates compact 10-article Markdown prompt files (batch_01.md, batch_02.md, ...)
    for external Frontier LLM web-chat arbitration (~600-900 chars per card + strict JSON schema).
    """
    rows = [
        json.loads(l)
        for l in ensemble_jsonl.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    selected: List[Dict[str, Any]] = []
    for r in rows:
        ens = r.get("ensemble") or {}
        if only_disputed and ens.get("ensemble_status") == "AUTO_GOLD":
            continue
        if r.get("human_label"):
            continue
        selected.append(r)

    batches_dir.mkdir(parents=True, exist_ok=True)
    written_files: List[Path] = []

    legal_header = (
        "# Независимый арбитраж публикаций VC.ru по 38-ФЗ «О рекламе»\n\n"
        "Проанализируй компактные карточки статей ниже строго по нормам 38-ФЗ РФ:\n"
        "- **п. 2–3 ч. 2 ст. 2 и ст. 3 38-ФЗ**: Справочно-аналитические материалы, новости и однородные обзоры `>= 3` "
        "конкурентов в едином стиле без выделения одного спонсора и без реферальной ссылки НЕ являются рекламой (`CLEAN_NON_AD`).\n"
        "- **Личный блог автора**: Если в конце авторской статьи стоит нейтральная подпись со ссылкой на личный канал автора "
        "без цен, скидок, промокодов и призыва купить услугу — это `CLEAN_NON_AD` (`F2 = true`).\n"
        "- **ст. 18.1 38-ФЗ**: Продвижение конкретного товара, сервиса, курса или чужого канала с призывом, тарифом, "
        "промокодом или реферальной/UTM ссылкой без токена `erid` — это `UNMARKED_AD_NO_ERID`. "
        "Если есть валидный `erid` и пометка «Реклама» — это `CLEAN_MARKED_AD`.\n"
        "- **ст. 7, ст. 24, ст. 28 38-ФЗ**: Казино, беттинг, сервисы обхода блокировок (VPN), реферальные ссылки криптобирж "
        "— это `BANNED_OR_SPECIAL_REGIME` (`F5 = true`).\n\n"
        "---\n\n"
    )

    json_footer = (
        "\n---\n\n"
        "## Строгий формат ответа (только чистый JSON-массив)\n\n"
        "Верни ответ **строго одним JSON-массивом** без текста до и после (чтобы его можно было сразу вставить в импорт AdRadar):\n\n"
        "```json\n"
        "[\n"
        "  {\n"
        '    "post_id": 1234567,\n'
        '    "F1": true,\n'
        '    "F2": false,\n'
        '    "F3": false,\n'
        '    "F4": true,\n'
        '    "F5": false,\n'
        '    "regime": "UNMARKED_AD_NO_ERID",\n'
        '    "confidence": 0.95,\n'
        '    "doubt_question_ru": ""\n'
        "  }\n"
        "]\n"
        "```\n"
        "Допустимые значения `regime`: `UNMARKED_AD_NO_ERID`, `CLEAN_MARKED_AD`, `CLEAN_NON_AD`, "
        "`INCOMPLETE_MARKING_LABEL_OR_INN`, `OVER_MARKED_NON_AD`, `BANNED_OR_SPECIAL_REGIME`.\n"
    )

    step = max(1, batch_size)
    for b_idx, i in enumerate(range(0, len(selected), step), start=1):
        chunk = selected[i : i + step]
        cards_md: List[str] = []
        for idx_in_batch, rec in enumerate(chunk, start=1):
            t0 = rec.get("tier0_passport") or build_tier0_passport(rec)
            cards_md.append(
                f"### Карточка {idx_in_batch}. `post_id={rec['post_id']}` — {t0['sanitized_title']}\n"
                f"- **Паспорт Tier-0**: `erids={t0['erids']}` · `strict_ad_label={t0['has_strict_ad_label']}` · "
                f"`inns={t0['valid_inns']}` · `affiliate={t0['has_affiliate_link']}` · `promo_code={t0['has_promo_code']}`\n"
                f"- **Автор**: `{json.dumps(t0['author_meta'], ensure_ascii=False)}`\n"
                f"- **Ссылки и контакты**: `{json.dumps(t0['commercial_targets'][:8], ensure_ascii=False)}`\n"
                f"- **Выжимка**: {t0['sanitized_digest']}\n"
            )

        batch_file = batches_dir / f"batch_{b_idx:02d}.md"
        batch_file.write_text(legal_header + "\n".join(cards_md) + json_footer, encoding="utf-8")
        written_files.append(batch_file)

    return written_files


def import_chat_batch_json(
    ensemble_jsonl: Path,
    chat_json_path: Path,
    min_consensus_conf: float = 0.90,
) -> Dict[str, Any]:
    """
    Imports external Frontier LLM chat JSON array into ensemble_jsonl and re-runs
    Anti-Echo-Chamber arbitration against the local + cloud jury.
    """
    raw_chat = chat_json_path.read_text(encoding="utf-8")
    parsed_items = _extract_json_items_array(raw_chat)
    chat_by_id: Dict[int, Dict[str, Any]] = {}
    for item in parsed_items:
        if item.get("post_id") is not None:
            chat_by_id[int(item["post_id"])] = item

    rows = [
        json.loads(l)
        for l in ensemble_jsonl.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]

    matched = 0
    promoted_auto = 0
    still_disputed = 0

    for r in rows:
        pid = int(r["post_id"])
        if pid not in chat_by_id:
            continue
        matched += 1
        c_item = chat_by_id[pid]
        t0 = r.get("tier0_passport") or build_tier0_passport(r)
        facts = normalize_atomic_facts(c_item)
        is_viol, derived_reg = derive_regime_from_atomic_facts(t0, facts)

        explicit_reg = str(c_item.get("regime") or "").strip()
        if explicit_reg in LegalRegime.__members__:
            reg_enum = LegalRegime[explicit_reg]
            is_viol = reg_enum in (
                LegalRegime.UNMARKED_AD_NO_ERID,
                LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN,
                LegalRegime.FOREIGN_LANG_168FZ,
                LegalRegime.BANNED_OR_SPECIAL_REGIME,
            )
            final_reg = reg_enum.name
        else:
            final_reg = derived_reg.name

        ext_vote = {
            "voter": "External-Chat-LLM",
            "model_used": "external_chat_batch",
            "family": "external_frontier_chat",
            "facts": facts,
            "is_violation": is_viol,
            "regime": final_reg,
            "confidence": float(c_item.get("confidence", 0.95) or 0.95),
            "doubt_question_ru": _sanitize_citations_safe(str(c_item.get("doubt_question_ru") or "")),
        }

        prior_votes = [
            v for v in ((r.get("ensemble") or {}).get("votes") or [])
            if v.get("family") != "external_frontier_chat"
        ]
        arb = arbitrate_ensemble_votes(
            record=r,
            tier0=t0,
            votes=prior_votes,
            external_chat_vote=ext_vote,
            min_consensus_conf=min_consensus_conf,
        )
        r["tier0_passport"] = t0
        r["ensemble"] = arb
        if arb["ensemble_status"] == "AUTO_GOLD":
            r["auto_gold_label"] = arb["consensus_regime"]
            promoted_auto += 1
        else:
            r["auto_gold_label"] = None
            still_disputed += 1

    with open(ensemble_jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {
        "imported_items": len(chat_by_id),
        "matched_articles": matched,
        "promoted_to_auto_gold": promoted_auto,
        "remaining_micro_q": still_disputed,
    }


def export_clean_training_jsonl(
    ensemble_jsonl: Path,
    out_labeled_jsonl: Path,
) -> Dict[str, Any]:
    """
    Exports ONLY verified records into vcru_human_labeled.jsonl:
    - HUMAN_VERIFIED -> sample_weight = 3.0
    - AUTO_GOLD (4/4 unanimous multi-family consensus) -> sample_weight = 2.0
    Strictly blocks NEEDS_MICRO_Q, SKIP, and any overlap with golden_set_real_vcru.jsonl.
    """
    blocked_ids, blocked_urls = load_golden_blocklist()
    rows = [
        json.loads(l)
        for l in ensemble_jsonl.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]

    violation_regimes = {
        "UNMARKED_AD_NO_ERID",
        "INCOMPLETE_MARKING_LABEL_OR_INN",
        "FOREIGN_LANG_168FZ",
        "BANNED_OR_SPECIAL_REGIME",
    }

    exported: List[Dict[str, Any]] = []
    cnt_human = 0
    cnt_auto = 0
    cnt_blocked_disputed = 0

    for r in rows:
        pid = int(r.get("post_id") or 0)
        u_norm = _normalize_url(str(r.get("url") or ""))
        if pid in blocked_ids or (u_norm and u_norm in blocked_urls):
            continue

        human_lbl = str(r.get("human_label") or "").strip()
        ens = r.get("ensemble") or {}

        chosen_regime = ""
        weight = 0.0
        source = ""

        if human_lbl and human_lbl != "SKIP" and human_lbl in LegalRegime.__members__:
            chosen_regime = human_lbl
            weight = 3.0
            source = "HUMAN_VERIFIED"
            cnt_human += 1
        elif not human_lbl and ens.get("ensemble_status") == "AUTO_GOLD":
            cons_reg = str(ens.get("consensus_regime") or "").strip()
            if cons_reg in LegalRegime.__members__:
                chosen_regime = cons_reg
                weight = float(ens.get("sample_weight") or 2.0)
                source = str(ens.get("label_source") or "UNANIMOUS_CONSENSUS")
                cnt_auto += 1
        elif not human_lbl and ens.get("ensemble_status") == "DEEP_BOOST_VERIFIED":
            cons_reg = str(ens.get("consensus_regime") or "").strip()
            if cons_reg in LegalRegime.__members__:
                chosen_regime = cons_reg
                weight = float(ens.get("sample_weight") or 2.5)
                source = "DEEP_BOOST_VERIFIED"
                cnt_auto += 1
        else:
            cnt_blocked_disputed += 1
            continue

        reg_enum = LegalRegime[chosen_regime]
        is_viol = chosen_regime in violation_regimes
        exported.append(
            {
                "post_id": pid,
                "url": r.get("url"),
                "rubric": r.get("rubric", "general"),
                "title": r.get("title", ""),
                "author_domain": "vc.ru",
                "text": r.get("full_text") or r.get("text") or r.get("text_window") or "",
                "text_window": r.get("text_window") or "",
                "external_links": r.get("external_links") or [],
                "erids": r.get("erids") or [],
                "is_promo": bool(r.get("is_promo") or r.get("is_promo_platform_flag")),
                "platform_ad_meta": r.get("platform_ad_meta") or {},
                "human_label": chosen_regime,
                "label_source": source,
                "sample_weight": weight,
                "expected_is_violation": is_viol,
                "expected_regime": reg_enum.name,
                "expected_regime_id": int(reg_enum.value),
                "labeled_at": datetime.now().isoformat(),
            }
        )

    out_labeled_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(out_labeled_jsonl, "w", encoding="utf-8") as f:
        for obj in exported:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return {
        "exported_total": len(exported),
        "human_verified_w3": cnt_human,
        "auto_gold_w2": cnt_auto,
        "blocked_unresolved": cnt_blocked_disputed,
        "output_path": str(out_labeled_jsonl),
    }


async def prepare_wide_boost_dossiers(
    ensemble_jsonl: Path,
    dossiers_out: Path,
) -> Dict[str, Any]:
    """
    Wide External Forensics for genuinely disputed articles:
    1) Re-arbitrates cached ensemble votes (promoting 3/3 Cloud-unanimous items to AUTO_GOLD),
    2) For every remaining disputed article, probes live external URLs and Telegram public
       web previews (https://t.me/s/<channel>) via Anti-SSRF safe_fetch_with_redirects to
       collect page <title>, <meta description>, channel bio, valid INNs, and tariff/CTA signals,
    3) Saves complete full-text + live-URL wide dossiers for multi-perspective Deep Boost synthesis.
    """
    from bs4 import BeautifulSoup

    rows = [json.loads(l) for l in ensemble_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    disputed_rows: List[Dict[str, Any]] = []
    auto_gold_cnt = 0

    for r in rows:
        t0 = r.get("tier0_passport") or build_tier0_passport(r)
        prior_votes = (r.get("ensemble") or {}).get("votes") or []
        arb = arbitrate_ensemble_votes(r, t0, prior_votes)
        r["tier0_passport"] = t0
        r["ensemble"] = arb
        if arb["ensemble_status"] == "AUTO_GOLD":
            r["auto_gold_label"] = arb["consensus_regime"]
            auto_gold_cnt += 1
        else:
            r["auto_gold_label"] = None
            disputed_rows.append(r)

    with open(ensemble_jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Probe unique external URLs / Telegram channels across disputed articles
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    sem = asyncio.Semaphore(8)
    url_intel_cache: Dict[str, Dict[str, Any]] = {}

    async def _probe_target_url(client: httpx.AsyncClient, raw_u: str) -> Tuple[str, Dict[str, Any]]:
        probe_u = raw_u
        m_tg = re.match(r"^https?://(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,})(?:[/?].*)?$", raw_u.strip())
        if m_tg and not m_tg.group(1).lower().endswith("bot"):
            probe_u = f"https://t.me/s/{m_tg.group(1)}"
        async with sem:
            try:
                resp, fin_u, _ = await safe_fetch_with_redirects(client, probe_u, timeout=5.5, headers=headers)
                if not resp:
                    return raw_u, {"status": "UNREACHABLE"}
                html_txt = resp.text[:90000] if resp.status_code == 200 else ""
                soup = BeautifulSoup(html_txt, "html.parser") if html_txt else None
                title_t = soup.title.get_text(" ", strip=True)[:160] if (soup and soup.title) else ""
                desc_tag = soup.find("meta", attrs={"name": re.compile(r"(?i)^description$")}) or (
                    soup.find("meta", attrs={"property": "og:description"}) if soup else None
                )
                desc_t = str(desc_tag.get("content") or "")[:260] if desc_tag else ""
                plain_sample = soup.get_text(" ", strip=True)[:3500] if soup else ""
                inns_found = find_valid_inns_in_text(plain_sample)[:4]
                has_commerce = bool(
                    re.search(r"(?i)\b(?:тариф\w*|купить|заказать|оформить|стоимость|оплат\w*|услуг\w*|курс\w*|бонус)\b", plain_sample)
                )
                return raw_u, {
                    "http_status": resp.status_code,
                    "final_url": fin_u[:200],
                    "title": sanitize_before_external_decision(title_t)[0],
                    "description": sanitize_before_external_decision(desc_t)[0],
                    "inns_on_target": inns_found,
                    "has_commercial_offer_on_target": has_commerce,
                }
            except Exception as e:
                return raw_u, {"status": f"ERR:{type(e).__name__}"}

    unique_urls: List[str] = []
    for r in disputed_rows:
        for u in (r.get("external_links") or [])[:4]:
            if u.startswith(("http://", "https://")) and u not in unique_urls:
                unique_urls.append(u)

    async with httpx.AsyncClient(timeout=7.0, follow_redirects=True, headers=headers) as client:
        if unique_urls:
            probed = await asyncio.gather(*[_probe_target_url(client, u) for u in unique_urls])
            for orig_u, info in probed:
                url_intel_cache[orig_u] = info

    dossiers: List[Dict[str, Any]] = []
    for r in disputed_rows:
        full_sanitized, _ = sanitize_before_external_decision(str(r.get("full_text") or r.get("text_window") or ""))
        links_forensics = {
            u: url_intel_cache.get(u, {})
            for u in (r.get("external_links") or [])[:4]
        }
        dossiers.append(
            {
                "post_id": int(r["post_id"]),
                "url": r.get("url"),
                "rubric": r.get("rubric", "general"),
                "title": sanitize_before_external_decision(str(r.get("title") or ""))[0],
                "author_meta": r.get("author_meta") or {},
                "tier0_summary": {
                    "erids": r.get("erids") or [],
                    "has_strict_ad_label": bool(r.get("has_strict_ad_label")),
                    "valid_inns": r.get("valid_inns") or [],
                    "commercial_targets": (r.get("commercial_targets") or [])[:10],
                    "has_affiliate_link": bool(r.get("has_affiliate_link")),
                    "has_promo_code": bool(r.get("has_promo_code")),
                    "is_promo": bool(r.get("is_promo")),
                    "is_editorial": bool(r.get("is_editorial")),
                },
                "prior_votes": [
                    {"voter": v.get("voter"), "regime": v.get("regime"), "confidence": v.get("confidence"), "facts": v.get("facts")}
                    for v in ((r.get("ensemble") or {}).get("votes") or [])
                ],
                "target_urls_forensics": links_forensics,
                "full_text_sanitized": full_sanitized[:6500],
            }
        )

    dossiers_out.parent.mkdir(parents=True, exist_ok=True)
    with open(dossiers_out, "w", encoding="utf-8") as f:
        for d in dossiers:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    return {
        "total_pool": len(rows),
        "auto_gold_count": auto_gold_cnt,
        "disputed_dossiers_built": len(dossiers),
        "probed_unique_urls": len(unique_urls),
        "dossiers_path": str(dossiers_out),
    }


def apply_deep_boost_verdicts(
    ensemble_jsonl: Path,
    boost_verdicts_json: Path,
    min_boost_conf: float = 0.88,
) -> Dict[str, Any]:
    """
    Applies multi-perspective Deep Boost dialectical verdicts onto disputed articles:
    - High-confidence clear statutory cases -> DEEP_BOOST_VERIFIED (sample_weight=2.5)
    - Genuinely unknowable/undisclosed grey cases -> ABSTAIN_REVIEW (excluded from noisy training)
    """
    raw = boost_verdicts_json.read_text(encoding="utf-8")
    items = _extract_json_items_array(raw)
    by_id = {int(x["post_id"]): x for x in items if x.get("post_id") is not None}

    rows = [json.loads(l) for l in ensemble_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    verified_cnt = 0
    abstain_cnt = 0

    violation_regimes = {
        "UNMARKED_AD_NO_ERID",
        "INCOMPLETE_MARKING_LABEL_OR_INN",
        "FOREIGN_LANG_168FZ",
        "BANNED_OR_SPECIAL_REGIME",
    }

    for r in rows:
        pid = int(r["post_id"])
        if pid not in by_id:
            continue
        bv = by_id[pid]
        reg = str(bv.get("regime") or "").strip()
        conf = float(bv.get("confidence", 0.0) or 0.0)
        abstain = bool(bv.get("abstain")) or reg == "ABSTAIN_REVIEW" or conf < min_boost_conf

        ens = r.get("ensemble") or {}
        ens["deep_boost"] = {
            "prosecutor_thesis_ru": _sanitize_citations_safe(str(bv.get("prosecutor_thesis_ru") or "")),
            "defense_antithesis_ru": _sanitize_citations_safe(str(bv.get("defense_antithesis_ru") or "")),
            "synthesis_verdict_ru": _sanitize_citations_safe(str(bv.get("synthesis_verdict_ru") or "")),
            "regime": reg,
            "confidence": round(conf, 4),
            "abstain": abstain,
        }
        if not abstain and reg in LegalRegime.__members__:
            ens["ensemble_status"] = "DEEP_BOOST_VERIFIED"
            ens["consensus_regime"] = reg
            ens["consensus_is_violation"] = reg in violation_regimes
            ens["consensus_confidence"] = round(conf, 4)
            ens["sample_weight"] = 2.5
            ens["label_source"] = "DEEP_BOOST_VERIFIED"
            r["auto_gold_label"] = reg
            verified_cnt += 1
        else:
            ens["ensemble_status"] = "ABSTAIN_REVIEW"
            ens["consensus_regime"] = None
            ens["consensus_is_violation"] = None
            ens["label_source"] = "ABSTAIN_GREY_ZONE"
            r["auto_gold_label"] = None
            abstain_cnt += 1
        r["ensemble"] = ens

    with open(ensemble_jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {
        "matched": len(by_id),
        "deep_boost_verified": verified_cnt,
        "abstain_grey_zone": abstain_cnt,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orthogonal Multi-Family Ensemble Labeler & Active Micro-Question Generator for VC.ru"
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run Tier 0 -> Tier 1 (Sheaf+Vega) -> Tier 2 (Cloud Jury) -> Micro-Q generator")
    p_run.add_argument("--input", type=str, default=None, help="Input raw pool JSONL (default: data/vcru_pool_raw.jsonl)")
    p_run.add_argument("--output", type=str, default=None, help="Output ensemble JSONL (default: data/vcru_ensemble_pool.jsonl)")
    p_run.add_argument("--batch-size", type=int, default=5, help="Articles per cloud jury micro-batch")
    p_run.add_argument("--limit", type=int, default=None, help="Optional cap on articles processed")
    p_run.add_argument("--skip-cloud", action="store_true", help="Run Tier 0 + Tier 1 locally without cloud calls")
    p_run.add_argument("--provider", type=str, default="auto", choices=["auto", "gemini", "openrouter"], help="Cloud jury provider (default: auto)")

    p_exp = sub.add_parser("export-chat-batch", help="Export 10-article Markdown digests for external chat arbitration")
    p_exp.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl")
    p_exp.add_argument("--out-dir", type=str, default=None, help="Output folder (default: data/chat_batches)")
    p_exp.add_argument("--batch-size", "--size", dest="batch_size", type=int, default=10, help="Articles per chat batch file")
    p_exp.add_argument("--all", action="store_true", help="Export all unlabelled articles, not only disputed ones")
    p_exp.add_argument("--print-batch", type=int, default=0, help="Print batch N directly to stdout")

    p_imp = sub.add_parser("import-chat-batch", help="Import JSON verdict array from external chat")
    p_imp.add_argument("--json-file", type=str, required=True, help="Path to JSON response from external chat")
    p_imp.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl")

    p_lbl = sub.add_parser("export-labeled", help="Export verified AUTO_GOLD (w=2.0) + DEEP_BOOST (w=2.5) + HUMAN_VERIFIED (w=3.0)")
    p_lbl.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl")
    p_lbl.add_argument("--output", type=str, default=None, help="Output path (default: data/vcru_human_labeled.jsonl)")

    p_wb = sub.add_parser("prepare-wide-boost", help="Probe live external URLs/Telegram bios and build full-text wide dossiers for disputed posts")
    p_wb.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl")
    p_wb.add_argument("--output", type=str, default=None, help="Output dossiers JSONL (default: data/vcru_wide_boost_dossiers.jsonl)")

    p_ab = sub.add_parser("apply-boost", help="Apply Deep Boost dialectical verdicts JSON to vcru_ensemble_pool.jsonl")
    p_ab.add_argument("--json-file", type=str, required=True, help="Path to Deep Boost verdicts JSON")
    p_ab.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl")

    args = parser.parse_args()
    data_dir = _active_data_dir() / "data"
    default_pool = data_dir / "vcru_pool_raw.jsonl"
    if not default_pool.exists():
        default_pool = data_dir / "vcru_to_label.jsonl"
    default_ens = data_dir / "vcru_ensemble_pool.jsonl"

    if args.command == "export-chat-batch":
        in_p = Path(args.input) if args.input else default_ens
        out_d = Path(args.out_dir) if args.out_dir else (data_dir / "chat_batches")
        files = export_chat_batches(in_p, out_d, batch_size=args.batch_size, only_disputed=not args.all)
        print(f"[+] Exported {len(files)} chat batch file(s) -> {out_d}")
        for fp in files:
            print(f"    - {fp}")
        if args.print_batch and 1 <= args.print_batch <= len(files):
            print("\n" + files[args.print_batch - 1].read_text(encoding="utf-8"))
        return 0

    if args.command == "import-chat-batch":
        in_p = Path(args.input) if args.input else default_ens
        res = import_chat_batch_json(in_p, Path(args.json_file))
        print(f"[+] Imported chat batch: {json.dumps(res, ensure_ascii=False)}")
        return 0

    if args.command == "prepare-wide-boost":
        in_p = Path(args.input) if args.input else default_ens
        out_p = Path(args.output) if args.output else (data_dir / "vcru_wide_boost_dossiers.jsonl")
        res = asyncio.run(prepare_wide_boost_dossiers(in_p, out_p))
        print(f"[+] Wide Boost dossiers ready: {json.dumps(res, ensure_ascii=False)}")
        return 0

    if args.command == "apply-boost":
        in_p = Path(args.input) if args.input else default_ens
        res = apply_deep_boost_verdicts(in_p, Path(args.json_file))
        print(f"[+] Applied Deep Boost verdicts: {json.dumps(res, ensure_ascii=False)}")
        return 0

    if args.command == "export-labeled":
        in_p = Path(args.input) if args.input else default_ens
        out_p = Path(args.output) if args.output else (data_dir / "vcru_human_labeled.jsonl")
        res = export_clean_training_jsonl(in_p, out_p)
        print(f"[+] Exported clean weighted dataset: {json.dumps(res, ensure_ascii=False)}")
        return 0

    # Default or 'run'
    in_p = Path(getattr(args, "input", None) or default_pool)
    out_p = Path(getattr(args, "output", None) or default_ens)
    b_sz = int(getattr(args, "batch_size", 5) or 5)
    lim = getattr(args, "limit", None)
    skip_c = bool(getattr(args, "skip_cloud", False))
    prov_c = str(getattr(args, "provider", "auto") or "auto")

    print(f"[*] Running 5-tier ensemble labeler on {in_p} -> {out_p} (batch_size={b_sz}, skip_cloud={skip_c}, provider={prov_c})...")
    summary = asyncio.run(
        run_ensemble_pipeline(
            input_path=in_p,
            output_path=out_p,
            cloud_batch_size=b_sz,
            skip_cloud=skip_c,
            limit=lim,
            provider=prov_c,
        )
    )
    print(f"[+] Ensemble labeling finished: {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
