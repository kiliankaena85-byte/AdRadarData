"""
Tier 2 Local Semantic Judge: Laya Vega-U 0.4.32 Grey-Zone Arbitration + Canonical 5-Fact Statutory Table.
Shared single source of truth for atomic legal facts (F1..F5) across:
- Runtime DualAuditor grey-zone arbitration (EXTERNAL_PUBLICATION ABSTAIN_REVIEW)
- Offline Ensemble Labeler (scripts/ensemble_labeler.py)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from legal_sheaf.schema import LegalRegime

logger = logging.getLogger("legal_sheaf.vega_grey_zone")

ATOMIC_FACT_KEYS: Tuple[str, ...] = (
    "F1_OBJECT_PROMOTED",
    "F2_AUTHOR_SELF_BIO",
    "F3_UNIFORM_MULTI_REVIEW",
    "F4_COMMERCIAL_CTA_OR_OFFER",
    "F5_BANNED_OR_SPECIAL",
)

SHORT_TO_FULL_FACT: Dict[str, str] = {
    "F1": "F1_OBJECT_PROMOTED",
    "F2": "F2_AUTHOR_SELF_BIO",
    "F3": "F3_UNIFORM_MULTI_REVIEW",
    "F4": "F4_COMMERCIAL_CTA_OR_OFFER",
    "F5": "F5_BANNED_OR_SPECIAL",
}


def normalize_atomic_facts(raw: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """Normalizes F1..F5 or F1_OBJECT_PROMOTED..F5_BANNED_OR_SPECIAL into canonical boolean dict."""
    src = raw or {}
    out: Dict[str, bool] = {}
    for short_k, full_k in SHORT_TO_FULL_FACT.items():
        val = src.get(full_k)
        if val is None:
            val = src.get(short_k)
        if isinstance(val, str):
            val_b = val.strip().lower() in ("true", "1", "yes", "да")
        else:
            val_b = bool(val)
        out[full_k] = val_b
    return out


def derive_regime_from_atomic_facts(
    tier0: Dict[str, Any],
    facts: Dict[str, Any],
) -> Tuple[bool, LegalRegime]:
    """
    Computes (expected_is_violation, LegalRegime) deterministically from Tier 0 immutable facts
    and the 5 atomic legal facts (Art. 2, 3, 7, 18.1 of 38-FZ).
    """
    f = normalize_atomic_facts(facts)
    f1 = f["F1_OBJECT_PROMOTED"]
    f2 = f["F2_AUTHOR_SELF_BIO"]
    f3 = f["F3_UNIFORM_MULTI_REVIEW"]
    f4 = f["F4_COMMERCIAL_CTA_OR_OFFER"]
    f5 = f["F5_BANNED_OR_SPECIAL"]

    ad_lbl = tier0.get("ad_label_status") or {}
    has_erid = bool(tier0.get("has_erid"))
    has_label = bool(tier0.get("has_strict_ad_label") or ad_lbl.get("has_strict_ad_label"))
    is_promo = bool(tier0.get("is_promo"))
    has_aff = bool(tier0.get("has_affiliate_link") or tier0.get("has_affiliate"))
    has_promo_code = bool(tier0.get("has_promo_code"))
    has_targets = (
        len(tier0.get("commercial_targets") or []) > 0
        or len(tier0.get("external_links") or []) > 0
        or bool(tier0.get("has_commercial_channel"))
    )

    # 1. Art. 7 / Art. 24 / Art. 28 38-FZ banned or special unlabelled regime
    if f5 and not (has_erid and has_label):
        return True, LegalRegime.BANNED_OR_SPECIAL_REGIME

    # 2. Lawfully marked official ad / partner material with valid ORD erid token
    if has_erid and (has_label or is_promo):
        if has_targets or f1 or f3 or f4:
            return False, LegalRegime.CLEAN_MARKED_AD
        return False, LegalRegime.OVER_MARKED_NON_AD

    # 3. Incomplete marking (erid present without mandatory 'Реклама' label)
    if has_erid and not has_label and not is_promo and (f1 or f4 or has_targets):
        return True, LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN

    # 4. Analytical multi-competitor review immunity (Art. 2(2)(3) 38-FZ) when no single-vendor promo/ref
    if f3 and not f4 and not has_aff and not has_promo_code:
        return False, LegalRegime.CLEAN_NON_AD

    # 5. Neutral author self-blog footer link without offer/CTA/prices/ref
    if f2 and not f4 and not has_aff and not has_promo_code:
        return False, LegalRegime.CLEAN_NON_AD

    # 6. Commercial promotion without erid (Art. 18.1 38-FZ)
    if (
        (f1 and (f4 or has_aff or has_promo_code))
        or (f4 and has_targets)
        or (has_aff and has_targets)
        or (has_promo_code and has_targets)
    ):
        return True, LegalRegime.UNMARKED_AD_NO_ERID

    if f1 and has_targets and not f2 and not f3:
        return True, LegalRegime.UNMARKED_AD_NO_ERID

    # 7. Over-marked non-ad vs clean editorial/analytical publication
    if has_label and not has_erid and not f1 and not f4 and not has_targets:
        return False, LegalRegime.OVER_MARKED_NON_AD

    return False, LegalRegime.CLEAN_NON_AD


def _build_grey_zone_questions() -> Dict[str, Dict[str, Any]]:
    return {
        "F1_OBJECT_PROMOTED": {
            "type": "noul",
            "instructions": (
                "Выделяется и продвигается ли в тексте или возле ссылки конкретный коммерческий продукт, "
                "сервис, компания, платный курс или коммерческий Telegram-бот/канал?"
            ),
        },
        "F2_AUTHOR_SELF_BIO": {
            "type": "noul",
            "instructions": (
                "Является ли единственная внешняя ссылка нейтральной авторской подписью на личный блог автора "
                "в конце информационной статьи без цены, скидки, промокода или рекламного предложения?"
            ),
        },
        "F3_UNIFORM_MULTI_REVIEW": {
            "type": "noul",
            "instructions": (
                "Является ли текст равномерным аналитическим обзором 3 и более конкурирующих сервисов/товаров "
                "без выделения одного заказного спонсора и без единой реферальной ссылки?"
            ),
        },
        "F4_COMMERCIAL_CTA_OR_OFFER": {
            "type": "noul",
            "instructions": (
                "Содержит ли фрагмент около ссылки призыв купить/заказать/попробовать/подключить сервис, "
                "описание тарифа, скидки, демо-доступа, промокод или партнерскую UTM/реферальную ссылку?"
            ),
        },
        "F5_BANNED_OR_SPECIAL": {
            "type": "noul",
            "instructions": (
                "Продвигает ли публикация запрещенные или специальные объекты в РФ "
                "(онлайн-казино, ставки на спорт, VPN-обход блокировок, рефералку криптообменника/биржи, "
                "медицинские услуги или кредиты без обязательных дисклеймеров)?"
            ),
        },
    }


def evaluate_grey_zone_with_vega(
    sensor: Any,
    title: str,
    url: str,
    external_links: List[str],
    link_windows: List[str],
    tier0_flags: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Tier 2 local non-autoregressive Vega-U judge for grey-zone ABSTAIN_REVIEW articles.
    Evaluates ONLY the title, external links, and +/-350-char link context windows.
    """
    t0 = time.perf_counter()
    if sensor is None:
        try:
            from verification_engine.sheaf_vega_verifier import VegaUSensor
            sensor = VegaUSensor(light_mode=False)
        except Exception as e:
            logger.warning("VegaUSensor unavailable for grey-zone routing: %s", e)
            return {
                "resolved": False,
                "is_violation": False,
                "regime": LegalRegime.CLEAN_NON_AD,
                "facts": {k: False for k in ATOMIC_FACT_KEYS},
                "confidence": 0.0,
                "abstain": True,
                "latency_ms": 0.0,
            }

    ad_lbl = tier0_flags.get("ad_label_status") or {}
    has_label = bool(tier0_flags.get("has_strict_ad_label") or ad_lbl.get("has_strict_ad_label"))
    has_aff = bool(tier0_flags.get("has_affiliate_link") or tier0_flags.get("has_affiliate"))
    has_promo_code = bool(tier0_flags.get("has_promo_code"))
    has_erid = bool(tier0_flags.get("has_erid"))

    windows_text = "\n---\n".join(w.strip() for w in (link_windows or []) if w and w.strip())
    state_str = (
        f"TITLE: {(title or '').strip()}\n"
        f"PAGE_URL: {(url or '').strip()}\n"
        f"EXTERNAL_LINKS: {', '.join((external_links or [])[:8]) or 'NONE'}\n"
        f"TIER0: erid={has_erid}, strict_ad_label={has_label}, "
        f"affiliate={has_aff}, promo_code={has_promo_code}\n"
        f"LINK_CONTEXT_WINDOWS:\n{windows_text[:1600]}"
    )

    if getattr(sensor, "light_mode", False):
        only_tg = (
            len(external_links) == 1
            and ("t.me/" in external_links[0].lower() or "telegram.me/" in external_links[0].lower())
        )
        pitch_or_cta = bool(
            re.search(
                r"(?i)(?:демо[-\s]*режим|тариф|скидк|промокод|купи|попробуй|услуг|наш\s+сервис|сайт\s*:)",
                windows_text,
            )
        )
        facts = {
            "F1_OBJECT_PROMOTED": bool(len(external_links) > 0 and (pitch_or_cta or not only_tg)),
            "F2_AUTHOR_SELF_BIO": bool(only_tg and not pitch_or_cta and not has_aff and not has_promo_code),
            "F3_UNIFORM_MULTI_REVIEW": False,
            "F4_COMMERCIAL_CTA_OR_OFFER": bool(pitch_or_cta or has_aff or has_promo_code),
            "F5_BANNED_OR_SPECIAL": bool(tier0_flags.get("has_banned_special")),
        }
        is_viol, reg = derive_regime_from_atomic_facts(
            {**tier0_flags, "external_links": external_links},
            facts,
        )
        return {
            "resolved": True,
            "is_violation": is_viol,
            "regime": reg,
            "facts": facts,
            "confidence": 0.92,
            "abstain": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    questions = _build_grey_zone_questions()
    facts: Dict[str, bool] = {}
    confs: List[float] = []
    any_abstain = False
    try:
        agent = sensor._get_agent()
        out = agent.predict(state=state_str[:10000], questions=questions)
        answers = out.get("answers") or {}
        for fk in ATOMIC_FACT_KEYS:
            ans = answers.get(fk) or {}
            pset = [str(x).lower() for x in (ans.get("prediction_set") or [])]
            facts[fk] = ("true" in pset) and ("false" not in pset)
            confs.append(float(ans.get("confidence", 0.0)))
            if ans.get("abstain") or ans.get("unbound") or len(pset) != 1:
                if fk in ("F1_OBJECT_PROMOTED", "F2_AUTHOR_SELF_BIO", "F4_COMMERCIAL_CTA_OR_OFFER"):
                    any_abstain = True
    except Exception as e:
        logger.warning("Vega-U grey-zone evaluation failed: %s", e)
        return {
            "resolved": False,
            "is_violation": False,
            "regime": LegalRegime.CLEAN_NON_AD,
            "facts": {k: False for k in ATOMIC_FACT_KEYS},
            "confidence": 0.0,
            "abstain": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }

    # Structural consistency guards on Vega-U binary outputs
    ext_links_clean = [str(u).strip() for u in (external_links or []) if u and str(u).strip()]
    only_tg_bio = (
        len(ext_links_clean) == 1
        and ("t.me/" in ext_links_clean[0].lower() or "telegram.me/" in ext_links_clean[0].lower())
        and not has_aff
        and not has_promo_code
    )
    if facts.get("F2_AUTHOR_SELF_BIO") and not only_tg_bio:
        facts["F2_AUTHOR_SELF_BIO"] = False

    unique_ext_hosts = set()
    for u in ext_links_clean:
        try:
            import urllib.parse as _up
            h = _up.urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
            if h and h != "vc.ru":
                unique_ext_hosts.add(h)
        except Exception:
            continue
    if facts.get("F3_UNIFORM_MULTI_REVIEW") and len(unique_ext_hosts) < 3:
        facts["F3_UNIFORM_MULTI_REVIEW"] = False

    is_viol, reg = derive_regime_from_atomic_facts(
        {**tier0_flags, "external_links": external_links},
        facts,
    )
    mean_conf = round(sum(confs) / max(1, len(confs)), 4) if confs else 0.0
    resolved = (not any_abstain) and (mean_conf >= 0.78)
    return {
        "resolved": resolved,
        "is_violation": is_viol,
        "regime": reg,
        "facts": facts,
        "confidence": mean_conf,
        "abstain": any_abstain,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }
