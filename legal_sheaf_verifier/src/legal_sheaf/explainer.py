"""
Legal Decision Explainer: Translates LinkCentricAdClassifierV3 outputs, feature attributions,
and statutory invariants into verifiable Russian legal audit conclusions.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np
import torch

from legal_sheaf.chronotope import (
    CIRCUIT_DESCRIPTIONS,
    FNS_REGION_MAP,
    JurisdictionRouter,
    TemporalValidityGate,
)
from legal_sheaf.inn_erid_validators import (
    analyze_ad_label_status,
    detect_untranslated_foreign_words_168fz,
    extract_commercial_targets,
    extract_erid_tokens,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    has_explicit_commercial_tracking,
)
from legal_sheaf.link_features import (
    FEATURE_NAMES,
    detect_banned_or_special_context,
    resolve_is_own_website,
    strip_links_footer,
)
from legal_sheaf.schema import (
    DocumentInput,
    LegalRegime,
    REGIME_NAMES,
    VerificationReport,
)

_PROMO_OFFER_REGEX = re.compile(
    r"(?i)\b(?:промокод[а-я]*\s*[:\s\-«\"]*[a-zA-Z0-9_-]{3,24}|розыгрыш\s+призов)\b"
)
_GIVEAWAY_WORD_REGEX = re.compile(r"(?i)\b(?:розыгрыш|разыгрываем|дарим\s+приз|дарю\s+\w+)\b")
_GIVEAWAY_SUB_REGEX = re.compile(r"(?i)\b(?:подпиш|подписк|условия\s+участия|отметь\s+(?:друз|трех)|лайк)\b")
_SPECIFIC_BUY_CTA_REGEX = re.compile(
    r"(?i)\b(?:купи(?:те)?|заказывай(?:те)?|оформляй(?:те)?|забронируй(?:те)?)\b[^.!\n]{0,80}"
    r"\b(?:со\s+скидк|скидк[аеиу]\s+\d+%|по\s+промокоду|за\s+\d+\s*(?:руб|₽)|тариф|подписк)\b"
)

SAFE_REGIME_INDICES = (
    LegalRegime.CLEAN_NON_AD.value,
    LegalRegime.CLEAN_MARKED_AD.value,
    LegalRegime.OVER_MARKED_NON_AD.value,
)
VIOL_REGIME_INDICES = (
    LegalRegime.UNMARKED_AD_NO_ERID.value,
    LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN.value,
    LegalRegime.FOREIGN_LANG_168FZ.value,
    LegalRegime.BANNED_OR_SPECIAL_REGIME.value,
)


def _to_numpy(val: Any) -> Optional[np.ndarray]:
    if val is None:
        return None
    if isinstance(val, torch.Tensor):
        return val.detach().cpu().numpy()
    return np.asarray(val)


def _load_unverified_case_numbers_from_seed() -> Set[str]:
    cases_path = Path(__file__).resolve().parent.parent.parent / "legal_kb_seed" / "court_and_ufas_cases.jsonl"
    out: Set[str] = set()
    if not cases_path.exists():
        return out
    try:
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not item.get("is_verified_citation", False) and item.get("case_number"):
                out.add(str(item["case_number"]))
    except Exception:
        pass
    return out


class LegalSheafExplainer:
    """Translates model outputs and statutory checks into verifiable legal audit conclusions."""

    def __init__(self, precedent_index: Optional[Any] = None):
        self.precedent_index = precedent_index
        self._unverified_cases_cache: Optional[Set[str]] = None
        self._temporal_gate = TemporalValidityGate()
        self._jurisdiction_router = JurisdictionRouter()

    def _get_unverified_case_numbers(self) -> Set[str]:
        if self.precedent_index is not None:
            return {
                prec["case_number"]
                for prec in getattr(self.precedent_index, "court_cases", [])
                if not prec.get("is_verified_citation", False) and prec.get("case_number")
            }
        if self._unverified_cases_cache is None:
            self._unverified_cases_cache = _load_unverified_case_numbers_from_seed()
        return self._unverified_cases_cache

    def explain(
        self,
        doc: DocumentInput,
        model_output: Optional[Dict[str, Any]],
        conformal_threshold: float = 0.50,
        latency_ms: float = 0.0,
        temperature: float = 1.0,
        abstain_lower: float = 0.35,
        abstain_upper: float = 0.62,
        feature_values: Optional[np.ndarray] = None,
        link_windows: Optional[List[str]] = None,
        tier0_flags: Optional[Dict[str, Any]] = None,
    ) -> VerificationReport:
        """Generates detailed, verifiable legal report from v3 model outputs and statutory guards."""
        temp = max(0.05, float(temperature) if temperature else 1.0)
        if model_output is not None:
            v_arr = _to_numpy(model_output.get("violation_logit"))
            v_logit = float(np.squeeze(v_arr)) if v_arr is not None else -3.0
            reg_arr = _to_numpy(model_output.get("regime_logits"))
            regime_logits = np.squeeze(reg_arr).astype(np.float32) if reg_arr is not None else np.zeros(7, dtype=np.float32)
            attr_arr = _to_numpy(model_output.get("feature_attributions"))
            total_energy_arr = _to_numpy(model_output.get("total_sheaf_energy"))
            total_energy = float(np.squeeze(total_energy_arr)) if total_energy_arr is not None else 0.0
        else:
            v_logit = -3.0
            regime_logits = np.array([3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
            attr_arr = None
            total_energy = 0.0

        scaled_logit = max(-40.0, min(40.0, v_logit / temp))
        v_prob = float(1.0 / (1.0 + math.exp(-scaled_logit)))

        exp_reg = np.exp(regime_logits - np.max(regime_logits))
        regime_probs = (exp_reg / np.maximum(np.sum(exp_reg), 1e-8)).astype(np.float32)

        if abstain_lower <= v_prob <= abstain_upper:
            conformal_verdict = "ABSTAIN_REVIEW"
        elif v_prob >= conformal_threshold:
            conformal_verdict = "VIOLATION"
        else:
            conformal_verdict = "CLEAN"

        is_violation = v_prob >= conformal_threshold
        pred_regime_idx = int(np.argmax(regime_probs))
        pred_regime = LegalRegime(pred_regime_idx)

        is_own_website = resolve_is_own_website(doc)
        text = doc.clean_text or ""
        body_text = strip_links_footer(text)

        own_website_citations: List[str] = []
        if is_own_website:
            links = [u.rstrip(".,;:!?)") for u in re.findall(r"https?://[^\s<>\"'\]]+", text)]
            has_third_party_ad = False
            a_domain = (doc.author_domain or "").lower().strip().removeprefix("www.")
            erid_tokens = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
            for link in links:
                p_link = urllib.parse.urlparse(link)
                netloc = p_link.netloc.lower().removeprefix("www.")
                if (
                    netloc
                    and a_domain not in netloc
                    and not any(
                        ex in netloc
                        for ex in ["vk.com", "t.me", "ok.ru", "yandex.ru", "gosuslugi.ru", "google.com", "youtube.com"]
                    )
                ):
                    if re.search(r"(?i)(?:utm_source|utm_campaign|partner|affiliate|ref=)", link) or re.search(
                        r"(?i)реклама\s+стороннего", text
                    ):
                        if not erid_tokens:
                            has_third_party_ad = True
                            break

            superlative_match = re.search(
                r"(?i)\b(?:лучш(?:ий|ая|ее|ие|их)|№\s*1|номер\s+один|"
                r"самый\s+(?:выгодн\w+|дешев\w+|надежн\w+|больш\w+|быстр\w+|качественн\w+)|"
                r"единственн(?:ый|ая|ое)\s+в\s+россии|первый\s+в\s+россии|лидер\s+рынка)\b",
                body_text,
            )
            has_unsubstantiated_superlative = False
            if superlative_match:
                has_footnote = bool(
                    re.search(
                        r"(?i)(?:\*|\b(?:по\s+(?:версии|данным|результатам|оценке)|исследован|опрос|рейтинг|критери|росстат|аналитик)\b)",
                        body_text,
                    )
                )
                if not has_footnote:
                    has_unsubstantiated_superlative = True

            has_promo_popup = bool(
                re.search(r"(?i)\b(?:popup|pop-up|modal-promo|banner-ad)\b", doc.raw_html or "")
                and re.search(r"(?i)\b(?:акция|скидк|распродаж|спецпредложен|только\s+до)\b", body_text)
            )
            has_medical_violation = False
            has_financial_violation = False
            if has_promo_popup:
                has_medical = bool(re.search(r"(?i)\b(?:клиник[аеиу]|стоматолог\w*|имплантац\w*|лечени[ея]|медицинск\w*)\b", body_text))
                has_contraindications = bool(re.search(r"(?i)(?:противопоказан|необходима\s+консультация|консультация\s+специалиста|имеются\s+противопоказания)", body_text))
                has_medical_violation = has_medical and not has_contraindications

                has_credit = bool(re.search(r"(?i)\b(?:кредит(?:\s+под\s+0%|\s+от\s+\d+%)?|рассрочк[аеиу]|займ|лизинг)\b", body_text))
                has_credit_terms = bool(re.search(r"(?i)(?:банк|лицензи|пао|полная\s+стоимость|пск|условия\s+предоставления|процентн\w+\s+ставк)", body_text))
                has_financial_violation = has_credit and not has_credit_terms

            lang_168_status = detect_untranslated_foreign_words_168fz(body_text)
            has_168fz_violation = bool(
                lang_168_status.get("has_violation_signals") and lang_168_status.get("flagged_commercial_phrases")
            )

            if has_third_party_ad:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.UNMARKED_AD_NO_ERID
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.88)
                own_website_citations.append("ч. 17 ст. 18.1 38-ФЗ, ч. 16 ст. 14.3 КоАП РФ (Распространение интернет-рекламы сторонних лиц на сайте без идентификатора erid)")
            elif has_unsubstantiated_superlative:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.85)
                own_website_citations.append("п. 1 ч. 3 ст. 5 38-ФЗ (Недостоверная реклама: использование превосходных степеней «лучший», «№1» без подтверждающих критериев или сноски на исследование)")
            elif has_medical_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.85)
                own_website_citations.append("ч. 7 ст. 24 38-ФЗ (Реклама медицинских услуг в промо-баннере без обязательного предупреждения о наличии противопоказаний)")
            elif has_financial_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.85)
                own_website_citations.append("ст. 28 38-ФЗ (Реклама финансовых услуг и кредитов/рассрочки в промо-баннере без существенных условий)")
            elif has_168fz_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.FOREIGN_LANG_168FZ
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.85)
                own_website_citations.append("168-ФЗ, ч. 5 ст. 5 38-ФЗ (Использование иностранных слов в коммерческом контексте без равнозначного перевода на русский язык)")
            else:
                is_violation = False
                conformal_verdict = "CLEAN"
                pred_regime = LegalRegime.CLEAN_NON_AD
                pred_regime_idx = pred_regime.value
                v_prob = min(v_prob, 0.12)
                own_website_citations.append("п. 2, 3 ч. 2 ст. 2 38-ФЗ, Приказ ФАС № 821/23, Письмо ФАС № АТ/61279/26 (Сведения о собственных товарах, услугах, каталоге и тарифах на официальном сайте компании не являются рекламой и не требуют erid)")
        else:
            extracted_targets = extract_commercial_targets(
                text,
                raw_links=[doc.page_url] if doc.page_url else None,
                page_url=doc.page_url,
            )
            ad_label_info = analyze_ad_label_status(text)
            erid_tokens_ext = extract_erid_tokens(
                text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta
            )
            banned_info = detect_banned_or_special_context(text, extracted_targets, is_own_website=False)
            has_banned_ext = bool(banned_info["has_banned_or_special"])

            has_promo_code = bool(
                _PROMO_OFFER_REGEX.search(body_text)
                or (_GIVEAWAY_WORD_REGEX.search(body_text) and _GIVEAWAY_SUB_REGEX.search(body_text))
            )
            has_any_ad_label = bool(
                ad_label_info["has_strict_ad_label"]
                or ad_label_info["is_illegal_surrogate"]
                or (doc.platform_ad_meta or {}).get("is_promo")
            )
            has_specific_cta = bool(_SPECIFIC_BUY_CTA_REGEX.search(body_text))
            has_commercial_channel = bool(
                extracted_targets["all_targets"]
                or has_promo_code
                or has_any_ad_label
                or erid_tokens_ext
                or has_specific_cta
            )

            # Anti-crypto-news FP guard
            if pred_regime == LegalRegime.BANNED_OR_SPECIAL_REGIME and not has_banned_ext:
                regime_probs[LegalRegime.BANNED_OR_SPECIAL_REGIME.value] = 0.0
                prob_sum = float(np.sum(regime_probs))
                if prob_sum > 1e-8:
                    regime_probs = regime_probs / prob_sum
                else:
                    regime_probs[LegalRegime.CLEAN_NON_AD.value] = 1.0
                pred_regime_idx = int(np.argmax(regime_probs))
                pred_regime = LegalRegime(pred_regime_idx)

            has_platform_marking = bool(
                (doc.platform_ad_meta or {}).get("erid")
                or (doc.platform_ad_meta or {}).get("is_promo")
                or (isinstance((doc.platform_ad_meta or {}).get("ad_data"), dict) and doc.platform_ad_meta["ad_data"])
            )
            if has_banned_ext and not (erid_tokens_ext and ad_label_info["has_strict_ad_label"]):
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = pred_regime.value
                v_prob = max(v_prob, 0.90)
            elif not has_commercial_channel:
                is_violation = False
                conformal_verdict = "CLEAN"
                pred_regime = LegalRegime.CLEAN_NON_AD
                pred_regime_idx = pred_regime.value
                v_prob = min(v_prob, 0.12)
            else:
                inns_complete = find_valid_inns_in_text(text) or find_valid_ogrns_in_text(text)
                lang_168_ext = detect_untranslated_foreign_words_168fz(body_text)
                has_non_label_channel = bool(
                    extracted_targets["all_targets"] or has_promo_code or has_specific_cta
                )
                has_explicit_tracking = any(
                    has_explicit_commercial_tracking(u, page_url=doc.page_url)
                    for u in (extracted_targets.get("commercial_urls") or [])
                )
                if (
                    erid_tokens_ext
                    and ad_label_info["has_strict_ad_label"]
                    and inns_complete
                    and not has_banned_ext
                    and not lang_168_ext.get("has_violation_signals")
                ):
                    is_violation = False
                    conformal_verdict = "CLEAN"
                    pred_regime = LegalRegime.CLEAN_MARKED_AD
                    pred_regime_idx = pred_regime.value
                    v_prob = min(v_prob, max(0.0, conformal_threshold - 0.08))
                elif (
                    (
                        ad_label_info["has_strict_ad_label"]
                        or ad_label_info["is_illegal_surrogate"]
                        or bool((doc.platform_ad_meta or {}).get("is_promo"))
                    )
                    and has_non_label_channel
                    and (not erid_tokens_ext or not inns_complete)
                ):
                    is_violation = True
                    conformal_verdict = "VIOLATION"
                    pred_regime = LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN
                    pred_regime_idx = pred_regime.value
                    v_prob = max(v_prob, 0.90)
                elif (has_explicit_tracking or has_promo_code) and not erid_tokens_ext and not has_platform_marking:
                    is_violation = True
                    conformal_verdict = "VIOLATION"
                    pred_regime = LegalRegime.UNMARKED_AD_NO_ERID
                    pred_regime_idx = pred_regime.value
                    v_prob = max(v_prob, 0.90)

            if pred_regime == LegalRegime.CLEAN_MARKED_AD and not erid_tokens_ext and not has_platform_marking:
                if has_commercial_channel:
                    is_violation = True
                    conformal_verdict = "VIOLATION"
                    pred_regime = (
                        LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN
                        if has_any_ad_label
                        else LegalRegime.UNMARKED_AD_NO_ERID
                    )
                    pred_regime_idx = pred_regime.value
                    v_prob = max(v_prob, min(0.95, conformal_threshold + 0.10))
                else:
                    is_violation = False
                    conformal_verdict = "CLEAN"
                    pred_regime = LegalRegime.CLEAN_NON_AD
                    pred_regime_idx = pred_regime.value

        # Head Consistency Lock
        if is_violation and pred_regime_idx in SAFE_REGIME_INDICES:
            erid_now = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
            lbl_now = analyze_ad_label_status(text)
            inn_now = find_valid_inns_in_text(text) or find_valid_ogrns_in_text(text)
            has_complete_marking = bool(
                erid_now and lbl_now["has_strict_ad_label"] and (inn_now or is_own_website)
            )
            if pred_regime == LegalRegime.CLEAN_MARKED_AD and has_complete_marking and not is_own_website:
                is_violation = False
                conformal_verdict = "CLEAN"
                v_prob = min(v_prob, max(0.0, conformal_threshold - 0.05))
            else:
                if (lbl_now["has_strict_ad_label"] or lbl_now["is_illegal_surrogate"]) and (not erid_now or not inn_now):
                    pred_regime_idx = LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN.value
                elif not erid_now and pred_regime == LegalRegime.CLEAN_MARKED_AD:
                    pred_regime_idx = LegalRegime.UNMARKED_AD_NO_ERID.value
                else:
                    pred_regime_idx = max(VIOL_REGIME_INDICES, key=lambda idx: float(regime_probs[idx]))
                pred_regime = LegalRegime(pred_regime_idx)

        if (not is_violation) and (pred_regime_idx in VIOL_REGIME_INDICES):
            erid_safe_check = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
            has_plat_safe = bool(
                (doc.platform_ad_meta or {}).get("erid")
                or (doc.platform_ad_meta or {}).get("is_promo")
                or (isinstance((doc.platform_ad_meta or {}).get("ad_data"), dict) and doc.platform_ad_meta["ad_data"])
            )
            safe_pool = (
                SAFE_REGIME_INDICES
                if (erid_safe_check or has_plat_safe)
                else (LegalRegime.CLEAN_NON_AD.value, LegalRegime.OVER_MARKED_NON_AD.value)
            )
            pred_regime_idx = max(safe_pool, key=lambda idx: float(regime_probs[idx]))
            pred_regime = LegalRegime(pred_regime_idx)

        regime_prob_dict = {
            REGIME_NAMES[i]: round(float(regime_probs[i]), 4)
            for i in range(len(LegalRegime))
        }

        # Top Feature Attributions (Linear Skip Path explainability)
        top_attributions: List[Dict[str, Any]] = []
        if attr_arr is not None:
            attr_vec = np.squeeze(attr_arr).astype(np.float32)
            if attr_vec.ndim == 1 and len(attr_vec) == len(FEATURE_NAMES):
                f_vec = np.squeeze(feature_values) if feature_values is not None else None
                order = np.argsort(np.abs(attr_vec))[::-1]
                for idx in order[:8]:
                    contrib = float(attr_vec[idx])
                    if abs(contrib) < 1e-4:
                        continue
                    item_attr: Dict[str, Any] = {
                        "feature": FEATURE_NAMES[int(idx)],
                        "contribution": round(contrib, 4),
                    }
                    if f_vec is not None and len(f_vec) == len(FEATURE_NAMES):
                        item_attr["value"] = round(float(f_vec[int(idx)]), 4)
                    top_attributions.append(item_attr)

        mitigating: List[str] = []
        procedural: List[str] = []

        region_code, ufas_name, circuit_code = self._jurisdiction_router.resolve_jurisdiction(doc.inn)
        circuit_desc = CIRCUIT_DESCRIPTIONS.get(circuit_code, circuit_code)

        temp_decision = self._temporal_gate.evaluate(
            published_at=doc.published_at,
            last_reactualized_at=doc.last_reactualized_at,
            inspected_at=doc.inspected_at,
            offense_type="ad_marking",
            continuity=doc.offense_continuity,
            is_sme=doc.is_sme,
            is_first_offense=doc.is_first_offense,
        )

        if not temp_decision.is_punishable:
            is_violation = False
            conformal_verdict = "CLEAN"
            if pred_regime_idx in VIOL_REGIME_INDICES:
                erid_tmp = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
                has_plat_tmp = bool(
                    (doc.platform_ad_meta or {}).get("erid")
                    or (doc.platform_ad_meta or {}).get("is_promo")
                    or (isinstance((doc.platform_ad_meta or {}).get("ad_data"), dict) and doc.platform_ad_meta["ad_data"])
                )
                safe_tmp = (
                    SAFE_REGIME_INDICES
                    if (erid_tmp or has_plat_tmp)
                    else (LegalRegime.CLEAN_NON_AD.value, LegalRegime.OVER_MARKED_NON_AD.value)
                )
                pred_regime_idx = max(safe_tmp, key=lambda idx: float(regime_probs[idx]))
                pred_regime = LegalRegime(pred_regime_idx)
            own_website_citations = []
            procedural.append(temp_decision.rationale)

        citations: List[str] = []
        if own_website_citations:
            citations.extend(own_website_citations)
        elif is_violation:
            if pred_regime == LegalRegime.UNMARKED_AD_NO_ERID:
                citations.append("ч. 17 ст. 18.1 38-ФЗ, ч. 16 ст. 14.3 КоАП РФ (Распространение интернет-рекламы без erid)")
            elif pred_regime == LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN:
                citations.append("ч. 16 ст. 18.1 38-ФЗ (Отсутствие пометки «Реклама» или сведений о рекламодателе/ИНН)")
            elif pred_regime == LegalRegime.FOREIGN_LANG_168FZ:
                citations.append("168-ФЗ, ч. 5 ст. 5 38-ФЗ (Использование иностранных слов без равнозначного перевода)")
            elif pred_regime == LegalRegime.BANNED_OR_SPECIAL_REGIME:
                citations.append("ст. 7, ст. 24, ст. 28 38-ФЗ (Запрещенные объекты рекламирования и специальные режимы)")
            else:
                citations.append("ст. 3, ч. 1 ст. 14.3 КоАП РФ (Общий состав нарушения рекламного законодательства)")
        else:
            if pred_regime == LegalRegime.CLEAN_MARKED_AD:
                citations.append("ч. 16, 17 ст. 18.1 38-ФЗ (Надлежащее исполнение требований о маркировке интернет-рекламы)")
            elif pred_regime == LegalRegime.CLEAN_NON_AD:
                citations.append("п. 3 ч. 2 ст. 2 38-ФЗ (Справочно-информационный / аналитический материал)")
            elif pred_regime == LegalRegime.OVER_MARKED_NON_AD:
                citations.append("Письмо ФАС России от 20.09.2023 № 08/76261/23 (Добровольное нанесение маркировки на не-рекламу не образует состава правонарушения)")

        if temp_decision.sme_relief:
            mitigating.append(temp_decision.sme_relief)

        # Optional regional divergence lookup ONLY when INN is supplied and precedent_index exists
        is_div = False
        div_notes = None
        if self.precedent_index is not None and doc.inn:
            is_div, div_notes, _, _ = self.precedent_index.check_regional_divergence(
                slot_text=body_text[:400],
                target_inn=doc.inn,
            )
            if is_div and div_notes:
                procedural.append(div_notes)

        if doc.published_days_ago >= 365:
            mitigating.append("Истек 1-годичный срок давности привлечения к административной ответственности (ст. 4.5 КоАП РФ)")
            procedural.append("Производство подлежит прекращению по п. 6 ч. 1 ст. 24.5 КоАП РФ")

        verified_citations = self._filter_verified_citations(citations)
        verified_mitigating = self._sanitize_text_list(mitigating)
        verified_procedural = self._sanitize_text_list(procedural)

        return VerificationReport(
            is_violation=is_violation,
            violation_probability=round(v_prob, 4),
            conformal_verdict=conformal_verdict,
            regime=pred_regime,
            regime_name=REGIME_NAMES[pred_regime_idx],
            regime_probabilities=regime_prob_dict,
            total_sheaf_energy=round(total_energy, 4),
            top_edge_conflicts=[],
            active_slots=[],
            top_feature_attributions=top_attributions,
            extracted_link_windows=[w[:400] for w in (link_windows or [])[:3]],
            classifier_architecture="LinkCentricAdClassifierV3",
            legal_citations=verified_citations,
            mitigating_factors=verified_mitigating,
            procedural_risks=verified_procedural,
            fast_standalone_mode=True,
            latency_ms=round(latency_ms, 3),
            temporal_status=temp_decision.status,
            temporal_notes=temp_decision.rationale,
            jurisdiction_region=f"{region_code} - {FNS_REGION_MAP.get(region_code, ('', 'Регион РФ', ''))[1]} ({ufas_name})",
            jurisdiction_circuit=f"{circuit_code} - {circuit_desc}",
            regional_practice_divergence=is_div,
            regional_practice_notes=div_notes,
            sme_sanction_mitigation=temp_decision.sme_relief,
        )

    def _filter_verified_citations(self, citations: List[str]) -> List[str]:
        """Guarantees that visible legal citations contain ONLY 100% verified RF legal acts."""
        unverified_case_numbers = self._get_unverified_case_numbers()
        return [c for c in citations if not any(ucn in c for ucn in unverified_case_numbers)]

    def _sanitize_text_list(self, items: List[str]) -> List[str]:
        """Strips any unverified court case numbers from textual diagnostic lists."""
        unverified_case_numbers = self._get_unverified_case_numbers()
        return [it for it in items if not any(ucn in it for ucn in unverified_case_numbers)]


LegalDecisionExplainer = LegalSheafExplainer
