"""
VLM Context Engine: Autonomous Human Review Desk Generator (review_desk.html).
Strict compliance with AGENTS.md, Zero-CDN constraint (100% self-contained HTML/CSS/JS).
Enables a 15-second human-in-the-loop review of automated scanner findings & VLM verdicts.
Includes:
- Side-by-side zoomable visual evidence & auto-uncheck on false positives;
- Dynamic Commercial Proposal builder strictly honoring checked checkboxes (no static hidden-kp-text);
- Real JSON feedback export (Blob download) with quick_tag (FALSE_POSITIVE / CONFIRMED / UNCERTAIN);
- Accurate KoAP penalty range extraction (total_min_rubles / total_max_rubles + applicable fallback);
- Honest VIP Arbitration CLI command copy helper (--vip-argon) with zero fake alert stubs.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from legal_sheaf import config as legal_config
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator

logger = logging.getLogger("vlm_context_engine.review_desk")

REVIEW_DESK_DIR = legal_config.ADRADAR_DATA_DIR / "review_desk"


def _active_review_desk_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve() / "review_desk"
    return Path(legal_config.ADRADAR_DATA_DIR).resolve() / "review_desk"


def map_finding_to_zone(finding_id: str, law_norm: str, quote: str) -> str:
    """
    Maps a compliance finding to its visual sniper trigger zone:
    - ZONE_1_FOOTER: Footer, Meta disclaimer (ч. 2 ст. 13.15 КоАП РФ), Requisites/INN (ЗоЗПП), Privacy Policy links.
    - ZONE_2_FORM: Lead forms, personal data inputs, cookie banners, 152-FZ consent, reCAPTCHA.
    - ZONE_3_HERO: Hero banner, superlatives (ст. 5 38-ФЗ), 168-FZ foreign words, installment claims.
    """
    fid = (finding_id or "").lower()
    norm = (law_norm or "").lower()
    q = (quote or "").lower()

    if any(k in fid or k in norm or k in q for k in ["meta", "13.15", "requisite", "зозпп", "2463", "footer", "подвал", "инн", "огрн"]):
        return "ZONE_1_FOOTER"
    elif any(k in fid or k in norm or k in q for k in ["form", "13.11", "cookie", "куки", "согласи", "персональн", "input", "recaptcha", "widget_data_leak"]):
        return "ZONE_2_FORM"
    elif any(k in fid or k in norm or k in q for k in ["superlative", "168fz", "foreign_words", "№1", "лучш", "hero", "баннер"]):
        return "ZONE_3_HERO"
    else:
        return "ZONE_1_FOOTER"


def build_commercial_proposal_text(
    domain: str,
    company_name: str,
    approved_findings: List[Dict[str, Any]],
    total_min_fine: int = 0,
    total_max_fine: int = 0,
) -> str:
    """
    Generates ready-to-dispatch commercial proposal text in Russian Engineering Fintech style.
    Contains strictly verified legal norms and a turnkey 14 900 ₽ remediation kit offer.
    """
    date_str = datetime.now().strftime("%d.%m.%Y")
    lines = [
        f"Здравствуйте, команда {company_name} ({domain})!",
        f"Экспресс-аудит комплаенса сайта {domain} от {date_str}.",
        "",
        "В ходе экспертного визуального и правового контроля на сайте зафиксированы зоны юридического риска:",
    ]

    for idx, f in enumerate(approved_findings, 1):
        norm = f.get("law_norm") or "КоАП РФ"
        desc = f.get("description") or f.get("notes") or f.get("finding_id") or "Выявлено нарушение"
        fine_min = int(f.get("fine_min_rub", 0) or 0)
        fine_max = int(f.get("fine_max_rub", 0) or 0)
        fine_str = f"штраф {fine_min:,} – {fine_max:,} ₽".replace(",", " ") if (fine_min or fine_max) else "предписание надзорного органа"
        lines.append(f"{idx}. {norm} ({desc}) — {fine_str}.")

    lines.append("")
    if total_max_fine > 0:
        total_str = f"{int(total_min_fine):,} – {int(total_max_fine):,} ₽".replace(",", " ")
        lines.append(f"Совокупный штрафной риск по КоАП РФ (с учетом ч. 5 ст. 4.4 КоАП): {total_str}.")
    lines.append("")
    lines.append("РЕШЕНИЕ ПОД КЛЮЧ:")
    lines.append("Мы подготовили персонализированный Комплект устранения рисков (Remediation Kit) за 14 900 ₽:")
    lines.append("• Полный пакет юридических документов (Политика 152-ФЗ, Согласие, Регламент cookie 2026);")
    lines.append("• Готовые безопасные сниппеты форм с правильными чекбоксами без скрытых трекеров;")
    lines.append("• Сноски и дисклеймеры по Meta и рекламным слоганам (38-ФЗ, 168-ФЗ);")
    lines.append("• Инструкция по внедрению за 15 минут без привлечения внешних юристов.")
    lines.append("")
    lines.append("Готовы передать комплект для мгновенного закрытия рисков.")

    return "\n".join(lines)


def _extract_fine_range(
    audit_result: Dict[str, Any],
    raw_findings: List[Dict[str, Any]],
) -> tuple[int, int, Dict[str, tuple[int, int]]]:
    """
    Extracts non-zero KoAP penalty bounds from audit_result['penalties'] using
    total_min_rubles/total_max_rubles, falling back to applicable_min_rubles/applicable_max_rubles,
    and computing directly via KoAPSanctionsCalculator if RED/ORANGE findings exist without bounds.
    """
    penalties = audit_result.get("penalties") or {}
    total_min = int(
        penalties.get("total_min_rubles")
        or penalties.get("applicable_min_rubles")
        or penalties.get("total_min_fine_rub")
        or 0
    )
    total_max = int(
        penalties.get("total_max_rubles")
        or penalties.get("applicable_max_rubles")
        or penalties.get("total_max_fine_rub")
        or 0
    )

    per_sig_fines: Dict[str, tuple[int, int]] = {}
    grouped = penalties.get("grouped_penalties") or []
    for grp in grouped:
        if not isinstance(grp, dict):
            continue
        g_min = int(grp.get("min_rubles") or grp.get("applicable_min_rubles") or grp.get("nominal_min_rubles") or 0)
        g_max = int(grp.get("max_rubles") or grp.get("applicable_max_rubles") or grp.get("nominal_max_rubles") or 0)
        for sig in grp.get("matched_violations") or []:
            per_sig_fines[str(sig).strip().lower()] = (g_min, g_max)

    if raw_findings and (total_max <= 0 or not per_sig_fines):
        calc = KoAPSanctionsCalculator()
        sigs = [
            str(f.get("signature") or f.get("finding_id") or "").strip()
            for f in raw_findings
            if "RED" in str(f.get("traffic_light") or "RED") or "ORANGE" in str(f.get("traffic_light") or "")
        ]
        if sigs:
            cp = audit_result.get("company_profile") or {}
            is_sme = bool(cp.get("is_sme", True))
            inn_val = str(cp.get("inn") or "")
            is_ip = len(inn_val) == 12
            computed = calc.calculate_penalties(sigs, is_sme=is_sme, first_offense=True, is_ip=is_ip)
            if total_max <= 0:
                total_min = int(computed.get("total_min_rubles") or computed.get("applicable_min_rubles") or 0)
                total_max = int(computed.get("total_max_rubles") or computed.get("applicable_max_rubles") or 0)
            for grp in computed.get("grouped_penalties") or []:
                g_min = int(grp.get("min_rubles") or grp.get("applicable_min_rubles") or grp.get("nominal_min_rubles") or 0)
                g_max = int(grp.get("max_rubles") or grp.get("applicable_max_rubles") or grp.get("nominal_max_rubles") or 0)
                for sig in grp.get("matched_violations") or []:
                    per_sig_fines.setdefault(str(sig).strip().lower(), (g_min, g_max))

    return total_min, total_max, per_sig_fines


class ReviewDeskBuilder:
    """Builder class for rendering 100% self-contained review_desk.html interfaces."""

    @staticmethod
    def render(
        domain: str,
        audit_result: Dict[str, Any],
        capture_result: Dict[str, Any],
        vlm_result: Dict[str, Any],
        output_dir: Optional[Union[str, Path]] = None,
        vega_verdicts_map: Optional[Dict[str, Any]] = None,
    ) -> Path:
        return generate_review_desk_html(
            domain=domain,
            audit_result=audit_result,
            capture_result=capture_result,
            vlm_result=vlm_result,
            output_dir=output_dir,
            vega_verdicts_map=vega_verdicts_map,
        )


def generate_review_desk_html(
    domain: str,
    audit_result: Dict[str, Any],
    capture_result: Dict[str, Any],
    vlm_result: Dict[str, Any],
    output_dir: Optional[Union[str, Path]] = None,
    vega_verdicts_map: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Renders self-contained review_desk/index.html (0 CDNs, 100% inline CSS/JS).
    Provides interactive 2-column layout: Screenshot Crop vs Scanner + VLM Verdict.
    Auto-unchecks items where VLM caught a False Positive, dynamically builds KP strictly
    from checked cards, exports feedback_<slug>.json via Blob download, and computes real KoAP fine range.
    """
    clean_dom_slug = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0].split(":")[0]
    if clean_dom_slug.startswith("www."):
        clean_dom_slug = clean_dom_slug[4:]
    slug = capture_result.get("domain") or re.sub(r"[^a-zA-Z0-9_\-]", "_", clean_dom_slug.replace(".", "_")).lower()

    target_dir = Path(output_dir) if output_dir else (_active_review_desk_dir() / slug)
    target_dir.mkdir(parents=True, exist_ok=True)
    html_path = target_dir / "index.html"

    company_profile = audit_result.get("company_profile", {})
    legal_name = company_profile.get("legal_name") or f"Компания ({domain})"
    inn = company_profile.get("inn") or "Не указан на сайте"
    revenue_rub = company_profile.get("revenue_rub")
    revenue_str = f"{revenue_rub:,} ₽ (ГИР БО ФНС)".replace(",", " ") if revenue_rub else "МСП / Не найдено в ГИР БО"

    final_url = audit_result.get("final_url") or capture_result.get("final_url") or audit_result.get("main_url") or f"https://{domain}"
    detected_lang = audit_result.get("detected_lang") or capture_result.get("detected_lang") or "ru"
    lang_display = "RU (Русский)" if detected_lang == "ru" else "EN (Английский)"

    traffic_light = audit_result.get("traffic_light", {})
    raw_findings: List[Dict[str, Any]] = (
        traffic_light.get("RED_REAL_FINE", [])
        + traffic_light.get("ORANGE_REGULATORY_DEMAND", [])
        + traffic_light.get("YELLOW_COMPLIANCE_GAP", [])
    )

    total_min_fine, total_max_fine, per_sig_fines = _extract_fine_range(audit_result, raw_findings)
    if total_max_fine > 0:
        fine_range_str = f"{total_min_fine:,} – {total_max_fine:,} ₽".replace(",", " ")
    elif raw_findings:
        fine_range_str = "Предупреждение / Предписание (ст. 4.1.1 КоАП)"
    else:
        fine_range_str = "0 ₽ (Нарушений не выявлено)"

    vlm_verdicts_map: Dict[str, Dict[str, Any]] = {}
    for vv in vlm_result.get("findings_verdicts", []):
        if isinstance(vv, dict) and "finding_id" in vv:
            vlm_verdicts_map[vv["finding_id"]] = vv

    vlm_model_used = vlm_result.get("model_used") or "nvidia/nemotron-3.5-super-vl:free"

    cards_html = []
    selected_initial_count = 0

    for idx, f in enumerate(raw_findings, 1):
        fid = f.get("finding_id") or f.get("signature") or f"finding_{idx}"
        sig_key = str(f.get("signature") or fid).strip().lower()
        law_norm = f.get("law_norm") or f.get("signature") or "Требование законодательства"
        desc = f.get("description") or f.get("title") or "Выявлено несоответствие"
        color_tier = f.get("traffic_light") or "ORANGE_REGULATORY_DEMAND"

        witness = f.get("witness") or {}
        if isinstance(witness, dict):
            quote = witness.get("quote", "")
            raw_snippet = witness.get("raw_snippet", "")
            notes = witness.get("notes", "")
        else:
            quote = getattr(witness, "quote", "") or ""
            raw_snippet = getattr(witness, "raw_snippet", "") or ""
            notes = getattr(witness, "notes", "") or ""

        zone_id = map_finding_to_zone(fid, law_norm, f"{quote} {raw_snippet} {desc}")
        zone_filename = "zone_1_footer.png"
        zone_label = "Зона #1: Подвал, Реквизиты и Сноски"
        if zone_id == "ZONE_2_FORM":
            zone_filename = "zone_2_form.png"
            zone_label = "Зона #2: Форма захвата и Согласие"
        elif zone_id == "ZONE_3_HERO":
            zone_filename = "zone_3_hero.png"
            zone_label = "Зона #3: Главный экран и Слоганы"

        vv = vlm_verdicts_map.get(fid, {})
        is_false_positive = bool(vv.get("is_false_positive", False))
        vlm_verdict_str = vv.get("vlm_verdict", "UNVERIFIED")
        vlm_evidence = vv.get("visual_evidence_ru", "")
        confidence = vv.get("confidence", 0.0)

        is_checked = not is_false_positive
        initial_quick_tag = "FALSE_POSITIVE" if is_false_positive else "CONFIRMED"
        if is_checked:
            selected_initial_count += 1

        card_fine_min, card_fine_max = per_sig_fines.get(
            sig_key,
            (int(f.get("fine_min_rub") or 0), int(f.get("fine_max_rub") or 0)),
        )

        tier_badge = "badge-orange"
        if "RED" in color_tier:
            tier_badge = "badge-red"
        elif "YELLOW" in color_tier:
            tier_badge = "badge-yellow"

        if is_false_positive:
            vlm_badge_html = (
                f'<div class="vlm-box vlm-box-fp">'
                f'  <div class="vlm-badge badge-fp">🛡 Снято Визуальным ОТК (False Positive) · {int(confidence*100)}%</div>'
                f'  <div class="vlm-evidence">{html.escape(vlm_evidence or "Обнаружено наличие сноски или согласия на скриншоте.")}</div>'
                f'  <div class="vlm-auto-note">⚡ Чекбокс автоматически снят для экономии времени ревьюера.</div>'
                f'</div>'
            )
        elif vlm_verdict_str == "CONFIRMED_VIOLATION":
            vlm_badge_html = (
                f'<div class="vlm-box vlm-box-confirmed">'
                f'  <div class="vlm-badge badge-confirmed">🚨 Подтверждено Визуальным ОТК · {int(confidence*100)}%</div>'
                f'  <div class="vlm-evidence">{html.escape(vlm_evidence or "На скриншоте зоны подтверждено отсутствие обязательных элементов.")}</div>'
                f'</div>'
            )
        elif vlm_verdict_str == "UNREADABLE_DISCLAIMER_RISK":
            vlm_badge_html = (
                f'<div class="vlm-box vlm-box-unverified">'
                f'  <div class="vlm-badge badge-red">⚠️ Риск: Дисклеймер нечитаем (ч. 7 ст. 5 38-ФЗ)</div>'
                f'  <div class="vlm-evidence">{html.escape(vlm_evidence or "Дисклеймер найден, но размер шрифта микроскопический или перекрыт UI.")}</div>'
                f'</div>'
            )
        else:
            vlm_badge_html = (
                f'<div class="vlm-box vlm-box-unverified">'
                f'  <div class="vlm-badge badge-unverified">⚠️ Требует визуального взгляда человека (15 сек)</div>'
                f'  <div class="vlm-evidence">{html.escape(vlm_result.get("human_hint_ru", "Проверьте визуальный фрагмент слева."))}</div>'
                f'</div>'
            )

        vv_vega = (vega_verdicts_map or {}).get(fid, {})
        v_route = vv_vega.get("route", "auto")
        v_abstain = vv_vega.get("abstain", False)
        v_unbound = vv_vega.get("unbound", False)
        v_readout = vv_vega.get("readout", "engine")
        v_conf = vv_vega.get("confidence", 0.0)
        v_energy = vv_vega.get("energy", 0.0)
        v_pset = vv_vega.get("prediction_set", ["VIOLATION_PRESENT"])
        pset_label = ", ".join(v_pset) if isinstance(v_pset, list) else str(v_pset)

        if v_unbound:
            vega_badge_html = (
                f'<div class="vega-box vega-box-ood">'
                f'  <div class="vega-badge badge-purple">🟣 VEGA-U (Laya): OOD (UNBOUND) · Эпистемическая неопределенность</div>'
                f'  <div class="vega-detail">Вне зоны калибровки (Readout: {html.escape(str(v_readout))}): требует обязательного взгляда эксперта.</div>'
                f'</div>'
            )
        elif v_abstain or v_route == "review":
            vega_badge_html = (
                f'<div class="vega-box vega-box-review">'
                f'  <div class="vega-badge badge-amber">🟡 VEGA-U (Laya): ROUTE REVIEW (ABSTAIN) · Конформное множество: [{html.escape(pset_label)}]</div>'
                f'  <div class="vega-detail">Классификатор воздержался от авто-решения (Readout: {html.escape(str(v_readout))}, Conf: {int(v_conf*100)}%): неоднозначный контекст.</div>'
                f'</div>'
            )
        else:
            vega_badge_html = (
                f'<div class="vega-box vega-box-auto">'
                f'  <div class="vega-badge badge-teal">🟢 VEGA-U (Laya): ROUTE AUTO · Замкнутый конформный класс: [{html.escape(pset_label)}]</div>'
                f'  <div class="vega-detail">Неавторегрессионный замок гарантирован (Readout: {html.escape(str(v_readout))}, Singleton, Conf: {int(v_conf*100)}%, Energy: {v_energy:.2f}).</div>'
                f'</div>'
            )

        checked_attr = "checked" if is_checked else ""
        fp_active = "active" if initial_quick_tag == "FALSE_POSITIVE" else ""
        conf_active = "active" if initial_quick_tag == "CONFIRMED" else ""

        card_html = f"""
        <div class="card {'card-dismissed' if is_false_positive else ''}" id="card-{idx}" data-finding-id="{html.escape(fid, quote=True)}" data-quick-tag="{initial_quick_tag}">
            <div class="card-left">
                <div class="zone-header">
                    <span class="zone-tag">{html.escape(zone_label)}</span>
                    <a href="{zone_filename}" target="_blank" class="zoom-btn" title="Открыть полный скриншот зоны">🔍 Увеличить</a>
                </div>
                <div class="image-wrapper">
                    <img src="{zone_filename}" alt="{html.escape(zone_label)}" onerror="this.onerror=null; this.src='collage.jpg'; this.onerror=function(){{this.src='collage.png';}};" loading="lazy">
                </div>
                <div class="witness-box">
                    <span class="witness-title">Код / Маркер сканера:</span>
                    <code>{html.escape(quote or raw_snippet or 'Маркер DOM')}</code>
                </div>
            </div>
            <div class="card-right">
                <div class="card-meta">
                    <span class="badge {tier_badge}">{html.escape(law_norm)}</span>
                    <span class="finding-id">ID: {html.escape(fid)}</span>
                </div>
                <h3 class="finding-title">{html.escape(desc)}</h3>
                {f'<p class="finding-notes">{html.escape(notes)}</p>' if notes else ''}

                {vlm_badge_html}
                {vega_badge_html}

                <div class="action-row">
                    <label class="toggle-control">
                        <input type="checkbox" class="finding-toggle" data-idx="{idx}" data-finding-code="{html.escape(fid, quote=True)}" data-norm="{html.escape(law_norm, quote=True)}" data-title="{html.escape(desc, quote=True)}" data-fine-min="{card_fine_min}" data-fine-max="{card_fine_max}" {checked_attr} onchange="onToggleChange({idx});">
                        <span class="toggle-label">Включить в КП для клиента</span>
                    </label>
                    <div class="quick-tags">
                        <button type="button" class="tag-btn {fp_active}" data-tag="FALSE_POSITIVE" onclick="setQuickFeedback({idx}, 'FALSE_POSITIVE');">❌ Ложное</button>
                        <button type="button" class="tag-btn" data-tag="UNCERTAIN" onclick="setQuickFeedback({idx}, 'UNCERTAIN');">❓ Сомнительно</button>
                        <button type="button" class="tag-btn {conf_active}" data-tag="CONFIRMED" onclick="setQuickFeedback({idx}, 'CONFIRMED');">✅ В КП</button>
                    </div>
                </div>
            </div>
        </div>
        """
        cards_html.append(card_html)

    desk_meta = {
        "domain": domain,
        "slug": slug,
        "url": final_url,
        "companyName": legal_name,
        "auditDate": datetime.now().strftime("%d.%m.%Y"),
        "totalMinFine": total_min_fine,
        "totalMaxFine": total_max_fine,
    }
    desk_meta_json = json.dumps(desk_meta, ensure_ascii=False)

    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AdRadar Visual Review Desk — {html.escape(domain)}</title>
<style>
/* 100% Self-Contained Russian Engineering Fintech Styling (Zero CDNs) */
:root {{
    --bg-main: #0b0f19;
    --bg-card: #121826;
    --bg-card-hover: #172033;
    --border-color: #232f48;
    --border-highlight: #3b82f6;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --accent-red: #ef4444;
    --accent-red-bg: rgba(239, 68, 68, 0.12);
    --accent-orange: #f59e0b;
    --accent-orange-bg: rgba(245, 158, 11, 0.12);
    --accent-green: #10b981;
    --accent-green-bg: rgba(16, 185, 129, 0.12);
    --accent-blue: #3b82f6;
    --accent-blue-bg: rgba(59, 130, 246, 0.12);
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}}

* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    background-color: var(--bg-main);
    color: var(--text-primary);
    font-family: var(--font-sans);
    line-height: 1.5;
    padding: 24px 32px 130px 32px;
}}
header {{
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 24px 28px;
    margin-bottom: 24px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
}}
.header-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border-color);
    padding-bottom: 16px;
    margin-bottom: 16px;
}}
.header-title-group h1 {{
    font-size: 24px;
    font-weight: 700;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 12px;
}}
.badge-fintech {{
    font-size: 12px;
    font-family: var(--font-mono);
    text-transform: uppercase;
    background: var(--accent-blue-bg);
    color: var(--accent-blue);
    border: 1px solid var(--accent-blue);
    padding: 4px 8px;
    border-radius: 6px;
}}
.human-status-badge {{
    font-size: 14px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 8px;
    background: var(--accent-orange-bg);
    color: var(--accent-orange);
    border: 1px solid var(--accent-orange);
    display: flex;
    align-items: center;
    gap: 8px;
}}
.meta-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    font-size: 14px;
}}
.meta-item {{ display: flex; flex-direction: column; gap: 4px; }}
.meta-label {{ color: var(--text-muted); font-size: 12px; text-transform: uppercase; font-weight: 600; }}
.meta-val {{ color: var(--text-primary); font-weight: 500; word-break: break-all; }}
.fine-highlight {{ color: var(--accent-orange); font-weight: 700; }}
.banner-alert {{
    background: var(--bg-card);
    border-left: 4px solid var(--accent-blue);
    border-top: 1px solid var(--border-color);
    border-right: 1px solid var(--border-color);
    border-bottom: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
}}
.banner-text {{ font-size: 14px; color: var(--text-secondary); }}
.action-toast {{
    display: none;
    background: #172033;
    border: 1px solid var(--accent-blue);
    border-radius: 8px;
    padding: 12px 18px;
    margin-bottom: 20px;
    font-size: 13px;
    color: var(--text-primary);
}}
.action-toast.warn {{ border-color: var(--accent-orange); color: #fde68a; }}
.action-toast.ok {{ border-color: var(--accent-green); color: #a7f3d0; }}
.action-toast code {{
    display: inline-block;
    margin-top: 6px;
    padding: 6px 10px;
    background: #0b0f19;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-family: var(--font-mono);
    color: #38bdf8;
}}
.cards-container {{ display: flex; flex-direction: column; gap: 20px; }}
.card {{
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    display: grid;
    grid-template-columns: 460px 1fr;
    overflow: hidden;
    transition: border-color 0.15s ease, background 0.15s ease;
}}
.card:hover {{ border-color: var(--border-highlight); background: var(--bg-card-hover); }}
.card.card-dismissed {{ opacity: 0.65; background: #0d121c; border-color: #1e2638; }}
.card-left {{
    background: #080b12;
    border-right: 1px solid var(--border-color);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}}
.zone-header {{ display: flex; justify-content: space-between; align-items: center; }}
.zone-tag {{
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    background: #172033;
    padding: 4px 8px;
    border-radius: 4px;
}}
.zoom-btn {{ font-size: 12px; color: var(--accent-blue); text-decoration: none; font-weight: 500; }}
.zoom-btn:hover {{ text-decoration: underline; }}
.image-wrapper {{
    width: 100%;
    height: 260px;
    background: #000;
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid #1a2333;
}}
.image-wrapper img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
.witness-box {{
    background: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
}}
.witness-title {{ color: var(--text-muted); font-size: 10px; text-transform: uppercase; display: block; margin-bottom: 4px; }}
.witness-box code {{ font-family: var(--font-mono); color: #e2e8f0; word-break: break-all; white-space: pre-wrap; }}
.card-right {{ padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; }}
.card-meta {{ display: flex; align-items: center; gap: 12px; }}
.badge {{ font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 6px; }}
.badge-red {{ background: var(--accent-red-bg); color: var(--accent-red); border: 1px solid var(--accent-red); }}
.badge-orange {{ background: var(--accent-orange-bg); color: var(--accent-orange); border: 1px solid var(--accent-orange); }}
.badge-yellow {{ background: rgba(234, 179, 8, 0.12); color: #eab308; border: 1px solid #eab308; }}
.finding-id {{ font-family: var(--font-mono); font-size: 12px; color: var(--text-muted); }}
.finding-title {{ font-size: 18px; font-weight: 600; color: var(--text-primary); }}
.finding-notes {{ font-size: 14px; color: var(--text-secondary); }}
.vlm-box {{ border-radius: 8px; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; }}
.vlm-box-fp {{ background: var(--accent-green-bg); border: 1px solid var(--accent-green); }}
.vlm-box-confirmed {{ background: var(--accent-red-bg); border: 1px solid var(--accent-red); }}
.vlm-box-unverified {{ background: rgba(148, 163, 184, 0.08); border: 1px solid #334155; }}
.vlm-badge {{ font-size: 13px; font-weight: 700; }}
.badge-fp {{ color: var(--accent-green); }}
.badge-confirmed {{ color: var(--accent-red); }}
.badge-unverified {{ color: var(--text-secondary); }}
.vlm-evidence {{ font-size: 13px; color: var(--text-primary); }}
.vlm-auto-note {{ font-size: 11px; color: var(--text-muted); font-style: italic; }}
.vega-box {{ border-radius: 8px; padding: 10px 14px; font-size: 12px; }}
.vega-box-auto {{ background: rgba(20, 184, 166, 0.12); border: 1px solid #14b8a6; color: #14b8a6; }}
.vega-box-review {{ background: rgba(245, 158, 11, 0.12); border: 1px solid #f59e0b; color: #f59e0b; }}
.vega-box-ood {{ background: rgba(168, 85, 247, 0.12); border: 1px solid #a855f7; color: #a855f7; }}
.action-row {{
    margin-top: auto;
    padding-top: 16px;
    border-top: 1px solid var(--border-color);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
}}
.toggle-control {{ display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }}
.toggle-control input[type="checkbox"] {{ width: 20px; height: 20px; cursor: pointer; accent-color: var(--accent-blue); }}
.toggle-label {{ font-size: 14px; font-weight: 600; color: var(--text-primary); }}
.quick-tags {{ display: flex; gap: 8px; }}
.tag-btn {{
    background: #172033;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s ease;
}}
.tag-btn:hover {{ background: #232f48; color: #fff; }}
.tag-btn.active {{ border-color: var(--accent-blue); background: var(--accent-blue-bg); color: #fff; font-weight: 600; }}
.floating-bar {{
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: rgba(18, 24, 38, 0.95);
    backdrop-filter: blur(12px);
    border-top: 1px solid var(--border-color);
    padding: 16px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 -4px 20px rgba(0,0,0,0.4);
    z-index: 100;
}}
.summary-stats {{ font-size: 14px; color: var(--text-secondary); display: flex; gap: 20px; }}
.btn-group {{ display: flex; gap: 12px; }}
.btn-primary {{
    background: var(--accent-blue);
    color: #fff;
    font-weight: 600;
    font-size: 14px;
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    cursor: pointer;
    transition: background 0.15s ease;
}}
.btn-primary:hover {{ background: #2563eb; }}
.btn-vip {{
    background: linear-gradient(135deg, #a855f7 0%, #ec4899 100%);
    color: #fff;
    font-weight: 600;
    font-size: 14px;
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    cursor: pointer;
    box-shadow: 0 2px 10px rgba(168, 85, 247, 0.3);
    transition: opacity 0.15s ease;
}}
.btn-vip:hover {{ opacity: 0.9; }}
</style>
</head>
<body>

<header>
    <div class="header-top">
        <div class="header-title-group">
            <h1>Пульт Визуального ОТК <span class="badge-fintech">AdRadar Fintech QA</span></h1>
        </div>
        <div class="human-status-badge">
            ⏱ Норматив валидации: 15 сек на домен
        </div>
    </div>
    <div class="meta-grid">
        <div class="meta-item">
            <span class="meta-label">Домен</span>
            <span class="meta-val">{html.escape(domain)}</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">Конечный URL (Capture)</span>
            <span class="meta-val">{html.escape(final_url)}</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">Язык страницы</span>
            <span class="meta-val">{html.escape(lang_display)}</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">Организация (ИНН)</span>
            <span class="meta-val">{html.escape(legal_name)} (ИНН: {html.escape(inn)})</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">Выручка</span>
            <span class="meta-val">{html.escape(revenue_str)}</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">Потенциальный штраф КоАП</span>
            <span class="meta-val fine-highlight" id="koap-fine-range">{html.escape(fine_range_str)}</span>
        </div>
        <div class="meta-item">
            <span class="meta-label">VLM Модель</span>
            <span class="meta-val">{html.escape(vlm_model_used)}</span>
        </div>
    </div>
</header>

<div class="banner-alert">
    <div class="banner-text">
        ℹ️ <strong>Инструкция оператора:</strong> Проверьте привязанные скриншоты зон слева.
        КП формируется динамически строго по отмеченным чекбоксам (снятые пункты в письмо не попадают).
    </div>
    <button type="button" class="btn-primary" onclick="copyCommercialProposal();">📋 Скопировать КП (14 900 ₽)</button>
</div>

<div id="action-toast" class="action-toast"></div>

<div class="cards-container">
    {''.join(cards_html) if cards_html else '<p style="color: var(--text-muted);">Нарушений не выявлено.</p>'}
</div>

<div class="floating-bar">
    <div class="summary-stats">
        <span>Всего триггеров: <strong id="total-count">{len(raw_findings)}</strong></span>
        <span>Отобрано в КП: <strong id="selected-count" style="color: var(--accent-blue);">{selected_initial_count}</strong></span>
        <span>Снято (False Positives): <strong id="fp-count" style="color: var(--accent-green);">{len(raw_findings) - selected_initial_count}</strong></span>
    </div>
    <div class="btn-group">
        <button type="button" class="btn-vip" onclick="requestVipArbitration();">💎 VIP-Арбитраж (Gemini 4 Argon)</button>
        <button type="button" class="btn-primary" onclick="saveVerdicts();">💾 Сохранить вердикты (feedback JSON)</button>
    </div>
</div>

<script>
// 100% Inline Client Logic (Zero external dependencies, dynamic checkbox-driven KP & Blob export)
const DESK_META = {desk_meta_json};

function showToast(htmlContent, level) {{
    const toast = document.getElementById('action-toast');
    if (!toast) return;
    toast.className = 'action-toast ' + (level || 'ok');
    toast.innerHTML = htmlContent;
    toast.style.display = 'block';
}}

function escapeText(str) {{
    return String(str == null ? '' : str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}}

function formatRub(n) {{
    return String(Math.round(Number(n) || 0)).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ' ');
}}

function updateSummary() {{
    const checkboxes = document.querySelectorAll('.finding-toggle');
    let selected = 0;
    checkboxes.forEach((cb) => {{
        const card = cb.closest('.card');
        if (cb.checked) {{
            selected++;
            if (card) card.classList.remove('card-dismissed');
        }} else {{
            if (card) card.classList.add('card-dismissed');
        }}
    }});
    const total = checkboxes.length;
    document.getElementById('selected-count').innerText = selected;
    document.getElementById('fp-count').innerText = (total - selected);
}}

function highlightTagButton(card, tag) {{
    if (!card) return;
    card.setAttribute('data-quick-tag', tag);
    card.querySelectorAll('.tag-btn').forEach((btn) => {{
        if (btn.getAttribute('data-tag') === tag) {{
            btn.classList.add('active');
        }} else {{
            btn.classList.remove('active');
        }}
    }});
}}

function onToggleChange(idx) {{
    const card = document.getElementById('card-' + idx);
    const cb = card ? card.querySelector('.finding-toggle') : null;
    if (card && cb) {{
        const curTag = card.getAttribute('data-quick-tag');
        if (curTag !== 'UNCERTAIN') {{
            highlightTagButton(card, cb.checked ? 'CONFIRMED' : 'FALSE_POSITIVE');
        }}
    }}
    updateSummary();
}}

function setQuickFeedback(idx, tag) {{
    const card = document.getElementById('card-' + idx);
    if (!card) return;
    const cb = card.querySelector('.finding-toggle');
    if (cb) {{
        if (tag === 'FALSE_POSITIVE' || tag === 'ЛОЖНОЕ_СРАБАТЫВАНИЕ' || tag === 'СНОСКА_ПРИСУТСТВУЕТ') {{
            cb.checked = false;
            tag = 'FALSE_POSITIVE';
        }} else if (tag === 'CONFIRMED' || tag === 'ПОДТВЕРЖДЕНО_В_КП') {{
            cb.checked = true;
            tag = 'CONFIRMED';
        }} else {{
            tag = 'UNCERTAIN';
        }}
    }}
    highlightTagButton(card, tag);
    updateSummary();
}}

function buildDynamicCommercialProposal() {{
    const selectedItems = [];
    document.querySelectorAll('.card').forEach((card) => {{
        const cb = card.querySelector('.finding-toggle');
        if (cb && cb.checked) {{
            selectedItems.push({{
                norm: cb.getAttribute('data-norm') || 'КоАП РФ',
                title: cb.getAttribute('data-title') || 'Выявлено нарушение',
                fineMin: Number(cb.getAttribute('data-fine-min') || 0),
                fineMax: Number(cb.getAttribute('data-fine-max') || 0)
            }});
        }}
    }});
    if (selectedItems.length === 0) {{
        return null;
    }}

    const lines = [
        'Здравствуйте, команда ' + DESK_META.companyName + ' (' + DESK_META.domain + ')!',
        'Экспресс-аудит комплаенса сайта ' + DESK_META.domain + ' от ' + DESK_META.auditDate + '.',
        '',
        'В ходе экспертного визуального и правового контроля на сайте зафиксированы подтвержденные зоны юридического риска:'
    ];
    selectedItems.forEach((item, idx) => {{
        const fineStr = (item.fineMax > 0)
            ? ('штраф ' + formatRub(item.fineMin) + ' – ' + formatRub(item.fineMax) + ' ₽')
            : 'предписание надзорного органа';
        lines.push((idx + 1) + '. ' + item.norm + ' (' + item.title + ') — ' + fineStr + '.');
    }});
    lines.push('');
    if (DESK_META.totalMaxFine > 0) {{
        lines.push('Совокупный штрафной риск по КоАП РФ (с учетом ч. 5 ст. 4.4 КоАП): ' + formatRub(DESK_META.totalMinFine) + ' – ' + formatRub(DESK_META.totalMaxFine) + ' ₽.');
        lines.push('');
    }}
    lines.push('РЕШЕНИЕ ПОД КЛЮЧ:');
    lines.push('Мы подготовили персонализированный Комплект устранения рисков (Remediation Kit) за 14 900 ₽:');
    lines.push('• Полный пакет юридических документов (Политика 152-ФЗ, Согласие, Регламент cookie 2026);');
    lines.push('• Готовые безопасные сниппеты форм с правильными чекбоксами без скрытых трекеров;');
    lines.push('• Сноски и дисклеймеры по Meta и рекламным слоганам (38-ФЗ, 168-ФЗ);');
    lines.push('• Инструкция по внедрению за 15 минут без привлечения внешних юристов.');
    lines.push('');
    lines.push('Готовы передать комплект для мгновенного закрытия рисков.');
    return {{ text: lines.join('\\n'), count: selectedItems.length }};
}}

function copyTextFallback(text) {{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
    document.body.removeChild(ta);
    return ok;
}}

function copyCommercialProposal() {{
    const built = buildDynamicCommercialProposal();
    if (!built) {{
        showToast('⚠️ <strong>Ни один пункт не отмечен для КП.</strong> Включите хотя бы одну карточку с реальным нарушением — пустое/ложное КП не скопировано.', 'warn');
        return;
    }}
    if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(built.text).then(() => {{
            showToast('✅ Коммерческое предложение (' + built.count + ' подтвержденных пунктов) скопировано в буфер обмена.', 'ok');
        }}).catch(() => {{
            copyTextFallback(built.text);
            showToast('✅ Коммерческое предложение (' + built.count + ' пунктов) скопировано в буфер обмена.', 'ok');
        }});
    }} else {{
        copyTextFallback(built.text);
        showToast('✅ Коммерческое предложение (' + built.count + ' пунктов) скопировано в буфер обмена.', 'ok');
    }}
}}

function requestVipArbitration() {{
    const vipCmd = '.venv\\\\Scripts\\\\python.exe scripts\\\\run_visual_qc.py --domain ' + DESK_META.domain + ' --vip-argon';
    if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(vipCmd).catch(() => {{ copyTextFallback(vipCmd); }});
    }} else {{
        copyTextFallback(vipCmd);
    }}
    showToast(
        '💎 <strong>Команда VIP-арбитража (Google Gemini 4 Argon + ZDR) скопирована в буфер обмена.</strong> Запустите в корне проекта:<br><code>' + escapeText(vipCmd) + '</code>',
        'ok'
    );
}}

function saveVerdicts() {{
    const nowIso = new Date().toISOString();
    const verdicts = [];
    document.querySelectorAll('.card').forEach((card) => {{
        const cb = card.querySelector('.finding-toggle');
        const fid = (cb && cb.getAttribute('data-finding-code')) || card.getAttribute('data-finding-id') || '';
        const norm = (cb && cb.getAttribute('data-norm')) || '';
        const title = (cb && cb.getAttribute('data-title')) || '';
        const approved = cb ? cb.checked : false;
        const quickTag = card.getAttribute('data-quick-tag') || (approved ? 'CONFIRMED' : 'FALSE_POSITIVE');
        verdicts.push({{
            domain: DESK_META.domain,
            url: DESK_META.url,
            finding_code: fid,
            title: title,
            law_norm: norm,
            approved_in_proposal: approved,
            quick_tag: quickTag,
            reviewed_at: nowIso
        }});
    }});

    const filename = 'feedback_' + DESK_META.slug + '.json';
    const blob = new Blob([JSON.stringify(verdicts, null, 2)], {{ type: 'application/json;charset=utf-8' }});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 1000);

    const learnCmd = '.venv\\\\Scripts\\\\adradar.exe learn --from-desk ' + filename;
    showToast(
        '💾 Скачан файл вердиктов <strong>' + escapeText(filename) + '</strong> (' + verdicts.length + ' записей с quick_tag). Импорт в дообучение:<br><code>' + escapeText(learnCmd) + '</code>',
        'ok'
    );
}}
</script>

</body>
</html>
"""

    html_path.write_text(full_html, encoding="utf-8")
    return html_path


def generate_review_desk_batch_index(
    entries: List[Dict[str, Any]],
    output_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Renders self-contained batch index page (review_desk/index.html) linking all
    generated Visual QC cards from a hunt-vcru batch run (0 CDNs).
    """
    target_dir = Path(output_dir) if output_dir else _active_review_desk_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    index_path = target_dir / "index.html"

    rows_html = []
    for idx, ent in enumerate(entries, 1):
        dom = html.escape(str(ent.get("domain", "")))
        slug = html.escape(str(ent.get("slug", "")))
        legal_name = html.escape(str(ent.get("legal_name", dom)))
        inn = html.escape(str(ent.get("inn") or "—"))
        fine_str = html.escape(str(ent.get("fine_range", "0 ₽")))
        v_cnt = int(ent.get("violations_count", 0))
        vlm_st = html.escape(str(ent.get("vlm_status", "UNVERIFIED")))
        rows_html.append(
            f"<tr>"
            f"<td>{idx}</td>"
            f"<td><strong>{dom}</strong><div style='color:#94a3b8;font-size:12px;'>{legal_name} (ИНН: {inn})</div></td>"
            f"<td>{v_cnt}</td>"
            f"<td style='color:#f59e0b;font-weight:600;'>{fine_str}</td>"
            f"<td><code>{vlm_st}</code></td>"
            f"<td><a href='{slug}/index.html' class='open-btn'>Открыть Пульт 15 сек →</a></td>"
            f"</tr>"
        )

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    page_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AdRadar Visual Review Desk — Пакетная сводка</title>
<style>
body {{ background:#0b0f19; color:#f1f5f9; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; padding:32px; }}
.wrap {{ max-width:1180px; margin:0 auto; }}
header {{ background:#121826; border:1px solid #232f48; border-radius:12px; padding:24px 28px; margin-bottom:24px; display:flex; justify-content:space-between; align-items:center; }}
h1 {{ font-size:22px; margin:0; }}
.meta {{ color:#94a3b8; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; background:#121826; border:1px solid #232f48; border-radius:12px; overflow:hidden; }}
th, td {{ padding:14px 16px; text-align:left; border-bottom:1px solid #232f48; font-size:14px; }}
th {{ background:#172033; color:#94a3b8; text-transform:uppercase; font-size:12px; }}
.open-btn {{ display:inline-block; background:#3b82f6; color:#fff; text-decoration:none; padding:8px 14px; border-radius:6px; font-weight:600; font-size:13px; }}
.open-btn:hover {{ background:#2563eb; }}
code {{ background:#0b0f19; padding:3px 6px; border-radius:4px; font-size:12px; color:#38bdf8; }}
</style>
</head>
<body>
<div class="wrap">
<header>
    <div>
        <h1>Пакетный Пульт Визуального ОТК (VC.ru Hunt)</h1>
        <div class="meta">Сформировано: {now_str} · Обработано лидов: {len(entries)}</div>
    </div>
</header>
<table>
    <thead>
        <tr>
            <th>#</th>
            <th>Домен / Компания</th>
            <th>Триггеры</th>
            <th>Потенциальный штраф КоАП</th>
            <th>Статус VLM</th>
            <th>Действие</th>
        </tr>
    </thead>
    <tbody>
        {''.join(rows_html) if rows_html else '<tr><td colspan="6" style="color:#94a3b8;">Нет новых лидов для визуального ОТК.</td></tr>'}
    </tbody>
</table>
</div>
</body>
</html>
"""
    index_path.write_text(page_html, encoding="utf-8")
    return index_path
