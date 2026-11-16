"""
VLM Context Engine: Context Rule Reconciler.
Enforces strict asymmetric authority boundaries on VLM decisions:
1. Technical code ground-truth (GTM, Yandex.Metrika, RKN registry status, FNS clone) is IRREVERSIBLE by VLM.
2. Canceling a violation (is_false_positive=True) strictly requires:
   element_visually_present == True AND readability_grade == "CLEARLY_READABLE" AND non-empty exact_visible_text.
3. If element is present but readability_grade in ("MICRO_FONT_UNREADABLE", "OVERLAPPED_BY_UI"),
   it is re-qualified into UNREADABLE_DISCLAIMER_RISK (ч. 7 ст. 5 38-ФЗ).
4. Contextual quarantine:
   - PORTFOLIO_CASE / HR_VACANCY -> suppresses 38-FZ advertising triggers.
   - B2B_CORPORATE_ONLY -> suppresses / demotes 168-FZ language triggers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from vlm_context_engine.openrouter_gateway import sanitize_model_citations

logger = logging.getLogger("vlm_context_engine.reconciler")


class ContextRuleReconciler:
    """
    Arbitrates scanner findings against multimodal VLM verdicts with legal domain safeguards.
    """

    IRREVERSIBLE_CODE_PROOFS: Set[str] = {
        "gtag_foreign_transfer",
        "google_tag_manager",
        "recaptcha_leak",
        "missing_inn_rkn_registry",
        "rkn_unregistered_operator",
        "fns_clone_suspicion",
        "cross_border_data_transfer",
        "abandoned_domain_risk",
    }

    ADVERTISING_SIGNATURES: Set[str] = {
        "art_5_advertising_superlative",
        "art_5_best_claim",
        "art_5_number_one_claim",
        "art_28_installment_missing_conditions",
        "reklama_missing_mark",
    }

    ANGLICISM_SIGNATURES: Set[str] = {
        "law_168_anglicism_untranslated",
        "untranslated_latin_slogan",
        "foreign_word_no_translation",
    }

    def reconcile(
        self,
        findings_verdicts: List[Dict[str, Any]],
        original_findings: List[Dict[str, Any]],
        page_genre: Optional[str] = None,
        allowed_citations: Optional[Set[str]] = None,
        allowed_koap: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Reconciles VLM verdicts against factual ground-truth, page genre, and readability grades.
        """
        allowed_cit = allowed_citations or set()
        allowed_kp = allowed_koap or set()

        orig_map = {
            (f.get("finding_id") or f.get("signature") or ""): f
            for f in original_findings
        }

        reconciled: List[Dict[str, Any]] = []

        genre = (page_genre or "").upper()
        is_portfolio_or_hr = genre in ("PORTFOLIO_CASE", "HR_VACANCY")
        is_b2b_only = genre == "B2B_CORPORATE_ONLY"

        for fv in findings_verdicts:
            fid = fv.get("finding_id", "")
            orig = orig_map.get(fid, {})
            sig = (orig.get("signature") or fid).lower()

            item = dict(fv)

            # 1. Asymmetric Lock: VLM cannot overrule technical code/registry evidence
            is_code_proof = any(cp in sig for cp in self.IRREVERSIBLE_CODE_PROOFS)
            if is_code_proof and item.get("is_false_positive") is True:
                logger.info(f"ContextRuleReconciler: Blocking VLM false positive on technical code proof '{fid}'.")
                item["is_false_positive"] = False
                item["vlm_verdict"] = "CONFIRMED_VIOLATION"
                item["reconciliation_note"] = (
                    "VLM не имеет полномочий отменять технические улики программного кода или госреестров "
                    "(Google Tag Manager, реестр операторов Роскомнадзора, проверка ФНС)."
                )

            # 2. Contextual Genre Quarantine
            # (A) Portfolio case / HR vacancy: Not commercial advertising under 38-FZ
            if is_portfolio_or_hr and any(ad_sig in sig for ad_sig in self.ADVERTISING_SIGNATURES):
                logger.info(f"ContextRuleReconciler: Quarantining 38-FZ trigger '{fid}' due to genre '{genre}'.")
                item["is_false_positive"] = True
                item["vlm_verdict"] = "FALSE_POSITIVE"
                item["reconciliation_note"] = (
                    f"Контекстный карантин: страница идентифицирована как {genre}. "
                    "Размещение кейсов в портфолио или вакансий не образует состав рекламного правонарушения по 38-ФЗ."
                )

            # (B) B2B corporate only: 168-FZ Anglicisms suppressed / demoted
            if is_b2b_only and any(ang_sig in sig for ang_sig in self.ANGLICISM_SIGNATURES):
                logger.info(f"ContextRuleReconciler: Quarantining 168-FZ trigger '{fid}' due to B2B corporate genre.")
                item["is_false_positive"] = True
                item["vlm_verdict"] = "FALSE_POSITIVE"
                item["reconciliation_note"] = (
                    "Контекстный карантин: на сайтах категории B2B_CORPORATE_ONLY англоязычная профессиональная "
                    "терминология допускается в деловом обороте."
                )

            # 3. Readability & False Positive Validation Lock
            if item.get("is_false_positive") is True and not (is_portfolio_or_hr or is_b2b_only):
                elem_present = item.get("element_visually_present")
                readability = str(item.get("readability_grade") or "").upper()
                exact_text = str(item.get("exact_visible_text") or "").strip()

                # Re-qualification to UNREADABLE_DISCLAIMER_RISK if micro-font or overlapped
                if elem_present and readability in ("MICRO_FONT_UNREADABLE", "OVERLAPPED_BY_UI"):
                    logger.info(f"ContextRuleReconciler: Re-qualifying '{fid}' to UNREADABLE_DISCLAIMER_RISK.")
                    item["is_false_positive"] = False
                    item["vlm_verdict"] = "UNREADABLE_DISCLAIMER_RISK"
                    item["readability_ok"] = False
                    item["reconciliation_note"] = (
                        "Внимание: дисклеймер формально присутствует, но выполнен микрошрифтом или перекрыт UI "
                        f"({readability}). Согласно ч. 7 ст. 5 38-ФЗ и позиции ВАС РФ № 51, нечитаемый текст "
                        "приравнивается к отсутствию информации (риск штрафа до 500 000 руб.)."
                    )
                elif not elem_present or readability != "CLEARLY_READABLE" or not exact_text:
                    # Missing prerequisite for clean false positive removal
                    logger.info(f"ContextRuleReconciler: Missing exact visible text or clear readability for '{fid}'. Demoting.")
                    item["is_false_positive"] = False
                    item["vlm_verdict"] = "UNVERIFIED"
                    item["reconciliation_note"] = (
                        "Снятие триггера отклонено: VLM не предоставил точный читаемый видимый текст "
                        f"(readability_grade={readability}, present={elem_present}, exact_text='{exact_text[:20]}')."
                    )

            # 4. Sanitize citations in text
            for field in ("visual_evidence_ru", "reconciliation_note", "exact_visible_text"):
                if field in item and isinstance(item[field], str):
                    item[field] = sanitize_model_citations(item[field], allowed_cit, allowed_kp)

            reconciled.append(item)

        return reconciled
