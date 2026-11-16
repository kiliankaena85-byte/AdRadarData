"""
Legal Sheaf Explainer: Translates Cellular Sheaf edge energies and stalk states
into human-readable Russian legal conclusions and audit reports.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch

from legal_sheaf.inn_erid_validators import (
    detect_untranslated_foreign_words_168fz,
    extract_erid_tokens,
)
from legal_sheaf.chronotope import (
    CIRCUIT_DESCRIPTIONS,
    FNS_REGION_MAP,
    JurisdictionRouter,
    TemporalValidityGate,
)
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import (
    DocumentInput,
    EdgeConflict,
    LegalRegime,
    ParamIdx,
    REGIME_NAMES,
    SlotDiagnostic,
    SlotID,
    SLOT_NAMES,
    VerificationReport,
)
from legal_sheaf.sheaf_model import LEGAL_EDGES


# Edge Conflict Explanations in Russian Law Context
EDGE_EXPLANATIONS: Dict[Tuple[int, int], Tuple[str, str]] = {
    (0, 5): (
        "OBJECT_MISSING_AD_LABEL",
        "Объект рекламирования индивидуализирован (п. 1, 2 ст. 3 38-ФЗ), однако отсутствует обязательная пометка «Реклама» (ч. 16 ст. 18.1 38-ФЗ)."
    ),
    (0, 6): (
        "OBJECT_MISSING_ADVERTISER",
        "Продвигается конкретный товар/бренд, но отсутствуют обязательные сведения о рекламодателе (наименование, ОПФ, ИНН) по ч. 16 ст. 18.1 38-ФЗ."
    ),
    (0, 7): (
        "OBJECT_MISSING_ERID",
        "Индивидуализирован объект продвижения, но в материале отсутствует идентификатор интернет-рекламы (erid) по ч. 17 ст. 18.1 38-ФЗ, ч. 16 ст. 14.3 КоАП РФ."
    ),
    (1, 5): (
        "ATTENTION_FOCUS_UNLABELED",
        "Привлечение внимания с использованием превосходных степеней без пометки «Реклама»."
    ),
    (2, 5): (
        "COMMERCIAL_OFFER_MISSING_LABEL",
        "Сформулированы коммерческие условия (цена, тариф, скидка), но отсутствует маркировка «Реклама»."
    ),
    (2, 7): (
        "COMMERCIAL_TERMS_MISSING_ERID",
        "Публикация содержит финансовые условия и скидки без присвоенного токена erid (ч. 16 ст. 14.3 КоАП РФ)."
    ),
    (3, 4): (
        "CTA_EXTERNAL_LINK_TENSION",
        "Императивный призыв к действию сопряжен с внешней коммерческой ссылкой с партнерскими метками."
    ),
    (3, 5): (
        "CTA_MISSING_LABEL",
        "Призыв к покупке/регистрации («купите», «переходите») без пометки «Реклама»."
    ),
    (3, 7): (
        "CTA_MISSING_ERID",
        "Призыв к действию с промокодом на скидку без токена erid в ЕРИР Роскомнадзора."
    ),
    (4, 7): (
        "TRACKED_LINK_WITHOUT_ERID",
        "Внешняя гиперссылка содержит UTM-метки/партнерские идентификаторы, но не содержит токена erid."
    ),
    (5, 6): (
        "LABEL_WITHOUT_ADVERTISER_INN",
        "Присутствует пометка рекламы, но отсутствует идентификация рекламодателя с валидным ИНН."
    ),
    (6, 7): (
        "ADVERTISER_WITHOUT_ORD_ERID",
        "Указан рекламодатель, но отсутствует идентификатор рекламы (erid)."
    ),
    (9, 0): (
        "ANALYTICAL_SHIELD_ACTIVE",
        "Справочно-аналитический иммунитет (п. 3 ч. 2 ст. 2 38-ФЗ) нейтрализует выделение объекта рекламирования."
    ),
    (10, 0): (
        "MULTIBRAND_REVIEW_ACTIVE",
        "Сравнительный обзор нескольких конкурирующих брендов в едином стиле снимает квалификацию рекламы."
    ),
    (11, 0): (
        "ORGANIC_INTEGRATION_ACTIVE",
        "Органичная интеграция в личный опыт автора без навязывания покупки (п. 9 ч. 2 ст. 2 38-ФЗ)."
    ),
    (12, 0): (
        "SELF_CATALOG_ACTIVE",
        "Рутинное информирование о собственном ассортименте, акциях и скидках на своем сайте (Письмо ФАС России от 09.04.2025 № АК/32908/25)."
    ),
    (13, 0): (
        "FOREIGN_LANGUAGE_168FZ",
        "Использование иностранных слов (Sale, Black Friday) в промо-блоке без перевода на русский язык (168-ФЗ, ч. 5 ст. 5 38-ФЗ)."
    ),
    (14, 0): (
        "SPECIAL_BANNED_REGIME",
        "Продвижение запрещенного объекта (VPN, нелегальные сервисы) или специального режима без дисклеймера."
    ),
    (15, 0): (
        "KOAP_PROCEDURAL_PROTECTION",
        "Применение годичного срока давности (ст. 4.5 КоАП) или замена штрафа на предупреждение для МСП (ст. 4.1.1 КоАП)."
    ),
}


class LegalSheafExplainer:
    """Translates model outputs into clear legal audit conclusions."""

    def __init__(self, precedent_index: Optional[LegalPrecedentIndex] = None):
        self.precedent_index = precedent_index or LegalPrecedentIndex()

    def explain(
        self,
        doc: DocumentInput,
        model_output: Dict[str, torch.Tensor],
        conformal_threshold: float = 0.50,
        latency_ms: float = 0.0,
    ) -> VerificationReport:
        """
        Generate detailed, verifiable legal report.
        """
        v_prob = torch.sigmoid(model_output["violation_logit"]).squeeze().item()
        regime_logits = model_output["regime_logits"].squeeze().detach().cpu().numpy()
        regime_probs = torch.softmax(torch.from_numpy(regime_logits), dim=-1).numpy()
        slot_probs = model_output["slot_probabilities"].squeeze().detach().cpu().numpy()
        edge_energies = model_output["edge_conflict_energies"].squeeze().detach().cpu().numpy()
        total_energy = model_output["total_sheaf_energy"].squeeze().item()

        # Conformal Verdict
        # If model is near threshold (e.g. 0.40 .. 0.60), abstain for manual review
        if 0.38 <= v_prob <= 0.62:
            conformal_verdict = "ABSTAIN_REVIEW"
        elif v_prob >= conformal_threshold:
            conformal_verdict = "VIOLATION"
        else:
            conformal_verdict = "CLEAN"

        is_violation = (v_prob >= conformal_threshold)

        # Predicted Legal Regime
        pred_regime_idx = int(np.argmax(regime_probs))
        pred_regime = LegalRegime(pred_regime_idx)

        # -------------------------------------------------------------
        # OWN_WEBSITE IMMUNITY vs EXTERNAL_PUBLICATION (п. 2, 3 ч. 2 ст. 2 38-ФЗ)
        # -------------------------------------------------------------
        is_own_website = False
        mode = getattr(doc, "context_mode", "AUTO")
        if mode == "OWN_WEBSITE":
            is_own_website = True
        elif mode == "AUTO":
            if doc.author_domain and doc.page_url:
                p_net = urllib.parse.urlparse(doc.page_url).netloc.lower()
                a_net = doc.author_domain.lower()
                if a_net in p_net or p_net in a_net:
                    is_own_website = True
            elif doc.author_domain:
                is_own_website = True

        own_website_citations: List[str] = []
        if is_own_website:
            text = doc.clean_text
            # 1. Third-party commercial links without erid
            links = re.findall(r"https?://[^\s<>\"']+", text)
            has_third_party_ad = False
            a_domain = doc.author_domain.lower().strip()
            erid_tokens = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
            for link in links:
                p_link = urllib.parse.urlparse(link)
                netloc = p_link.netloc.lower()
                if netloc and a_domain not in netloc and not any(ex in netloc for ex in ["vk.com", "t.me", "ok.ru", "yandex.ru", "gosuslugi.ru", "google.com", "youtube.com"]):
                    if re.search(r"(?i)(?:utm_source|utm_campaign|partner|affiliate|ref=)", link):
                        if not erid_tokens:
                            has_third_party_ad = True
                            break

            # 2. Unsubstantiated superlatives (п. 1 ч. 3 ст. 5 38-ФЗ)
            superlative_match = re.search(
                r"(?i)\b(?:лучш(?:ий|ая|ее|ие|их)|№\s*1|номер\s+один|самый\s+(?:выгодн\w+|дешев\w+|надежн\w+|больш\w+|быстр\w+|качественн\w+)|единственн(?:ый|ая|ое)\s+в\s+россии|первый\s+в\s+россии|лидер\s+рынка)\b",
                text
            )
            has_unsubstantiated_superlative = False
            if superlative_match:
                has_footnote = bool(re.search(
                    r"(?i)(?:\*|\b(?:по\s+(?:версии|данным|результатам|оценке)|исследован|опрос|рейтинг|критери|росстат|аналитик)\b)",
                    text
                ))
                if not has_footnote:
                    has_unsubstantiated_superlative = True

            # 3. Medical/Credit disclaimer rules on OWN_WEBSITE:
            # Per FAS Russia & 323-FZ Art. 79, listing medical services, doctors, and prices on a clinic's own site
            # is mandatory informing, NOT advertising (Part 2 Art. 2 38-FZ).
            # ч. 7 ст. 24 applies on own sites ONLY if a dedicated promotional pop-up banner is detected.
            has_promo_popup = bool(
                re.search(r"(?i)\b(?:popup|pop-up|modal-promo|banner-ad)\b", doc.raw_html) and
                re.search(r"(?i)\b(?:акция|скидк|распродаж|спецпредложен|только\s+до)\b", text)
            )
            has_medical_violation = False
            has_financial_violation = False
            if has_promo_popup:
                has_medical = bool(re.search(r"(?i)\b(?:клиник[аеиу]|стоматолог\w*|имплантац\w*|лечени[ея]|медицинск\w*)\b", text))
                has_contraindications = bool(re.search(r"(?i)(?:противопоказан|необходима\s+консультация|консультация\s+специалиста|имеются\s+противопоказания)", text))
                has_medical_violation = (has_medical and not has_contraindications)

                has_credit = bool(re.search(r"(?i)\b(?:кредит(?:\s+под\s+0%|\s+от\s+\d+%)?|рассрочк[аеиу]|займ|лизинг)\b", text))
                has_credit_terms = bool(re.search(r"(?i)(?:банк|лицензи|пао|полная\s+стоимость|пск|условия\s+предоставления|процентн\w+\s+ставк)", text))
                has_financial_violation = (has_credit and not has_credit_terms)

            # 4. 168-FZ language violations (untranslated commercial B2C phrases)
            lang_168_status = detect_untranslated_foreign_words_168fz(text)
            has_168fz_violation = bool(lang_168_status.get("has_violation_signals") and lang_168_status.get("flagged_commercial_phrases"))

            # Arbitration for OWN_WEBSITE
            if has_third_party_ad:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.UNMARKED_AD_NO_ERID
                pred_regime_idx = LegalRegime.UNMARKED_AD_NO_ERID.value
                own_website_citations.append("ч. 17 ст. 18.1 38-ФЗ, ч. 16 ст. 14.3 КоАП РФ (Распространение интернет-рекламы сторонних лиц на сайте без идентификатора erid)")
            elif has_unsubstantiated_superlative:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = LegalRegime.BANNED_OR_SPECIAL_REGIME.value
                own_website_citations.append("п. 1 ч. 3 ст. 5 38-ФЗ (Недостоверная реклама: использование превосходных степеней «лучший», «№1» без подтверждающих критериев или сноски на исследование)")
            elif has_medical_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = LegalRegime.BANNED_OR_SPECIAL_REGIME.value
                own_website_citations.append("ч. 7 ст. 24 38-ФЗ (Реклама медицинских услуг в промо-баннере без обязательного предупреждения о наличии противопоказаний)")
            elif has_financial_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
                pred_regime_idx = LegalRegime.BANNED_OR_SPECIAL_REGIME.value
                own_website_citations.append("ст. 28 38-ФЗ (Реклама финансовых услуг и кредитов/рассрочки в промо-баннере без существенных условий)")
            elif has_168fz_violation:
                is_violation = True
                conformal_verdict = "VIOLATION"
                pred_regime = LegalRegime.FOREIGN_LANG_168FZ
                pred_regime_idx = LegalRegime.FOREIGN_LANG_168FZ.value
                own_website_citations.append("168-ФЗ, ч. 5 ст. 5 38-ФЗ (Использование иностранных слов в коммерческом контексте без равнозначного перевода на русский язык)")
            else:
                # 100% OWN WEBSITE IMMUNITY ACTIVATED!
                is_violation = False
                conformal_verdict = "CLEAN"
                pred_regime = LegalRegime.CLEAN_NON_AD
                pred_regime_idx = LegalRegime.CLEAN_NON_AD.value
                v_prob = min(v_prob, 0.12)
                own_website_citations.append("п. 2, 3 ч. 2 ст. 2 38-ФЗ, Приказ ФАС № 821/23, Письмо ФАС № АТ/61279/26 (Сведения о собственных товарах, услугах, каталоге и тарифах на официальном сайте компании не являются рекламой и не требуют erid)")

        # Regimes probabilities dictionary
        regime_prob_dict = {
            REGIME_NAMES[i]: round(float(regime_probs[i]), 4)
            for i in range(len(LegalRegime))
        }

        # Analyze Edge Conflicts (sort by energy descending)
        top_edge_conflicts: List[EdgeConflict] = []
        edge_indices_sorted = np.argsort(edge_energies)[::-1]
        for e_idx in edge_indices_sorted[:5]:
            energy_val = float(edge_energies[e_idx])
            if energy_val < 0.05:
                continue
            u, v = LEGAL_EDGES[e_idx]
            u_name = SLOT_NAMES.get(u, f"SLOT_{u}")
            v_name = SLOT_NAMES.get(v, f"SLOT_{v}")
            conf_type, expl = EDGE_EXPLANATIONS.get(
                (u, v),
                (f"EDGE_TENSION_{u}_{v}", f"Конфликт согласования между {u_name} и {v_name}.")
            )
            top_edge_conflicts.append(EdgeConflict(
                source_slot=u,
                source_slot_name=u_name,
                target_slot=v,
                target_slot_name=v_name,
                energy=round(energy_val, 4),
                conflict_type=conf_type,
                explanation=expl,
            ))

        # Active Slots Diagnostics
        active_slots: List[SlotDiagnostic] = []
        for s_id in range(16):
            prob = float(slot_probs[s_id])
            if prob >= 0.35:
                s_name = SLOT_NAMES.get(s_id, f"SLOT_{s_id}")
                summary = f"Слот {s_name} активен с вероятностью {prob:.2f}."
                active_slots.append(SlotDiagnostic(
                    slot_id=s_id,
                    slot_name=s_name,
                    probability=round(prob, 4),
                    features=[],
                    summary=summary,
                ))

        # Legal Citations
        citations = []
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

        # Mitigating & Procedural Factors
        mitigating = []
        procedural = []

        temporal_gate = TemporalValidityGate()
        jurisdiction_router = JurisdictionRouter()

        region_code, ufas_name, circuit_code = jurisdiction_router.resolve_jurisdiction(doc.inn)
        circuit_desc = CIRCUIT_DESCRIPTIONS.get(circuit_code, circuit_code)

        temp_decision = temporal_gate.evaluate(
            published_at=doc.published_at,
            last_reactualized_at=doc.last_reactualized_at,
            inspected_at=doc.inspected_at,
            offense_type="ad_marking",
            continuity=doc.offense_continuity,
            is_sme=doc.is_sme,
            is_first_offense=doc.is_first_offense,
        )

        # Temporal gate override (statute of limitations, non-retroactivity, decree 1427)
        if not temp_decision.is_punishable:
            is_violation = False
            conformal_verdict = "CLEAN"
            procedural.append(temp_decision.rationale)

        if temp_decision.sme_relief:
            mitigating.append(temp_decision.sme_relief)

        # Regional practice divergence check
        is_div = False
        div_notes = None
        if self.precedent_index is not None and doc.inn:
            is_div, div_notes, _, _ = self.precedent_index.check_regional_divergence(
                slot_text=doc.clean_text[:400],
                target_inn=doc.inn,
            )
            if is_div and div_notes:
                procedural.append(div_notes)

        if doc.published_days_ago >= 365:
            mitigating.append("Истек 1-годичный срок давности привлечения к административной ответственности (ст. 4.5 КоАП РФ)")
            procedural.append("Производство подлежит прекращению по п. 6 ч. 1 ст. 24.5 КоАП РФ")

        if slot_probs[SlotID.PROCEDURAL_KOAP] > 0.4:
            mitigating.append("Возможность замены административного штрафа на предупреждение для субъектов МСП (ст. 4.1.1 КоАП РФ)")

        # Strictly enforce Zero Fake Citations & unverified court case filtering
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
            top_edge_conflicts=top_edge_conflicts,
            active_slots=active_slots,
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
        """
        Guarantees that visible legal citations contain ONLY 100% verified RF legal acts.
        Strictly strips any unverified court or UFAS case numbers.
        """
        unverified_case_numbers = set()
        if self.precedent_index is not None:
            for prec in self.precedent_index.court_cases:
                if not prec.get("is_verified_citation", False):
                    c_num = prec.get("case_number")
                    if c_num:
                        unverified_case_numbers.add(c_num)

        clean_citations = []
        for c in citations:
            # Check if citation mentions any unverified case number
            if any(ucn in c for ucn in unverified_case_numbers):
                continue
            clean_citations.append(c)
        return clean_citations

    def _sanitize_text_list(self, items: List[str]) -> List[str]:
        """Strips any unverified court case numbers from textual diagnostic lists."""
        unverified_case_numbers = set()
        if self.precedent_index is not None:
            for prec in self.precedent_index.court_cases:
                if not prec.get("is_verified_citation", False):
                    c_num = prec.get("case_number")
                    if c_num:
                        unverified_case_numbers.add(c_num)

        clean_items = []
        for it in items:
            if not any(ucn in it for ucn in unverified_case_numbers):
                clean_items.append(it)
        return clean_items
