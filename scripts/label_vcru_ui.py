"""
Generates a 100% self-contained, zero-CDN offline HTML UI (C:\\AdRadarData\\data\\labeler.html)
— «Пульт Микро-Вопросов v2» — for rapid 5-second/card active resolution of VC.ru ensemble
disagreements, external 10-card chat JSON import, and clean weighted dataset export
(vcru_human_labeled.jsonl with weight=3.0 HUMAN_VERIFIED / weight=2.0 UNANIMOUS_CONSENSUS,
plus optional split of human-verified cards into golden_candidates_vcru.jsonl).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "legal_sheaf_verifier" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from legal_sheaf.config import ADRADAR_DATA_DIR
from legal_sheaf.schema import LegalRegime


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    return Path(env_dir).resolve() if env_dir else Path(ADRADAR_DATA_DIR).resolve()


def _safe_json_for_html_script(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False)
    return raw.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_labeler_html(records: List[Dict[str, Any]], source_jsonl_path: Path) -> str:
    dataset_json = _safe_json_for_html_script(records)
    regime_map = _safe_json_for_html_script(
        {
            "UNMARKED_AD_NO_ERID": {
                "expected_is_violation": True,
                "expected_regime": LegalRegime.UNMARKED_AD_NO_ERID.name,
                "expected_regime_id": int(LegalRegime.UNMARKED_AD_NO_ERID.value),
            },
            "INCOMPLETE_MARKING_LABEL_OR_INN": {
                "expected_is_violation": True,
                "expected_regime": LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN.name,
                "expected_regime_id": int(LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN.value),
            },
            "FOREIGN_LANG_168FZ": {
                "expected_is_violation": True,
                "expected_regime": LegalRegime.FOREIGN_LANG_168FZ.name,
                "expected_regime_id": int(LegalRegime.FOREIGN_LANG_168FZ.value),
            },
            "BANNED_OR_SPECIAL_REGIME": {
                "expected_is_violation": True,
                "expected_regime": LegalRegime.BANNED_OR_SPECIAL_REGIME.name,
                "expected_regime_id": int(LegalRegime.BANNED_OR_SPECIAL_REGIME.value),
            },
            "CLEAN_MARKED_AD": {
                "expected_is_violation": False,
                "expected_regime": LegalRegime.CLEAN_MARKED_AD.name,
                "expected_regime_id": int(LegalRegime.CLEAN_MARKED_AD.value),
            },
            "CLEAN_NON_AD": {
                "expected_is_violation": False,
                "expected_regime": LegalRegime.CLEAN_NON_AD.name,
                "expected_regime_id": int(LegalRegime.CLEAN_NON_AD.value),
            },
            "OVER_MARKED_NON_AD": {
                "expected_is_violation": False,
                "expected_regime": LegalRegime.OVER_MARKED_NON_AD.name,
                "expected_regime_id": int(LegalRegime.OVER_MARKED_NON_AD.value),
            },
            "SKIP": {
                "expected_is_violation": None,
                "expected_regime": "SKIP",
                "expected_regime_id": -1,
            },
        }
    )
    src_display = str(source_jsonl_path).replace("\\", "\\\\")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AdRadar Пульт Микро-Вопросов v2 (Ансамблевая разметка VC.ru)</title>
<style>
:root {{
    --bg: #0b0f19;
    --card: #121826;
    --card2: #172033;
    --border: #232f48;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --red: #ef4444;
    --green: #22c55e;
    --blue: #3b82f6;
    --amber: #f59e0b;
    --purple: #a855f7;
}}
* {{ box-sizing: border-box; }}
body {{
    margin: 0;
    padding: 20px 24px 108px 24px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
}}
.container {{ max-width: 1140px; margin: 0 auto; }}
.topbar {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 14px;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
}}
.topbar h1 {{ margin: 0; font-size: 19px; font-weight: 700; }}
.sub {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 13px; }}
.pill {{
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 4px 11px;
    font-weight: 600;
}}
.pill-gold {{ border-color: rgba(34,197,94,0.5); color: #86efac; }}
.pill-dispute {{ border-color: rgba(245,158,11,0.55); color: #fde68a; }}

.tabs-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 14px;
    align-items: center;
    justify-content: space-between;
}}
.tab-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.tab-btn {{
    padding: 9px 16px;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted);
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
}}
.tab-btn.active {{
    background: rgba(59,130,246,0.2);
    border-color: var(--blue);
    color: #fff;
}}

.import-drawer {{
    display: none;
    background: var(--card);
    border: 1px solid var(--amber);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 16px;
}}
.import-drawer.open {{ display: block; }}
.import-textarea {{
    width: 100%;
    min-height: 130px;
    background: #090d16;
    color: #e2e8f0;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-family: Consolas, monospace;
    font-size: 12px;
    margin: 8px 0 10px 0;
}}

.progress-wrap {{
    width: 100%;
    height: 8px;
    background: #172033;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 14px;
}}
.progress-fill {{
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--blue), var(--green));
    transition: width 0.2s ease;
}}
.nav-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-bottom: 16px;
}}
.nav-dot {{
    min-width: 28px;
    height: 28px;
    padding: 0 5px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
}}
.nav-dot.active {{ border-color: #fff; color: #fff; box-shadow: 0 0 0 2px rgba(59,130,246,0.45); }}
.nav-dot.auto-gold {{ background: rgba(34,197,94,0.14); border-color: #15803d; color: #86efac; }}
.nav-dot.lbl-UNMARKED_AD_NO_ERID, .nav-dot.lbl-BANNED_OR_SPECIAL_REGIME, .nav-dot.lbl-INCOMPLETE_MARKING_LABEL_OR_INN {{
    background: rgba(239,68,68,0.24); border-color: var(--red); color: #fecaca;
}}
.nav-dot.lbl-CLEAN_MARKED_AD, .nav-dot.lbl-OVER_MARKED_NON_AD {{
    background: rgba(59,130,246,0.24); border-color: var(--blue); color: #bfdbfe;
}}
.nav-dot.lbl-CLEAN_NON_AD {{
    background: rgba(34,197,94,0.26); border-color: var(--green); color: #bbf7d0;
}}
.nav-dot.lbl-SKIP {{
    background: rgba(148,163,184,0.22); border-color: #64748b; color: #e2e8f0;
}}

.main-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 24px;
}}
.card-head {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 12px;
}}
.post-title {{ font-size: 19px; font-weight: 700; margin: 0 0 4px 0; }}
.orig-link {{ color: #60a5fa; text-decoration: none; font-size: 13px; word-break: break-all; }}
.orig-link:hover {{ text-decoration: underline; }}

.micro-box {{
    background: linear-gradient(180deg, rgba(245,158,11,0.09), rgba(23,32,51,0.75));
    border: 1px solid rgba(245,158,11,0.45);
    border-radius: 12px;
    padding: 16px 18px;
    margin-bottom: 16px;
}}
.jury-line {{
    font-size: 12px;
    color: #cbd5e1;
    background: #0b101d;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    margin-bottom: 12px;
}}
.quote-snippet {{
    font-size: 15px;
    line-height: 1.55;
    background: #080c14;
    border-left: 4px solid var(--amber);
    padding: 12px 14px;
    border-radius: 6px;
    color: #f8fafc;
    margin-bottom: 12px;
}}
.hl-trig {{
    background: rgba(245,158,11,0.32);
    color: #fef08a;
    padding: 1px 5px;
    border-radius: 4px;
    font-weight: 700;
}}
.micro-q-title {{
    font-size: 15px;
    font-weight: 700;
    color: #fde68a;
    margin-bottom: 12px;
}}
.micro-btn-row {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 10px;
}}
.micro-btn {{
    padding: 14px 16px;
    border-radius: 10px;
    border: 2px solid var(--border);
    background: var(--card2);
    color: var(--text);
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    text-align: left;
}}
.micro-btn:hover {{ border-color: #94a3b8; transform: translateY(-1px); }}
.micro-btn.opt1 {{ border-color: rgba(34,197,94,0.45); }}
.micro-btn.opt2 {{ border-color: rgba(239,68,68,0.45); }}
.micro-btn.selected {{ box-shadow: 0 0 0 2px #fff; }}

.meta-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px;
    margin: 12px 0;
}}
.meta-box {{
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
    font-size: 13px;
}}
.meta-lbl {{ color: var(--muted); font-size: 11px; text-transform: uppercase; margin-bottom: 4px; }}
.badge-ok {{ color: var(--green); font-weight: 700; }}
.badge-warn {{ color: var(--amber); font-weight: 700; }}

.links-box {{
    background: #0d1320;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 12px;
    font-size: 12px;
}}
.link-item {{ display: block; padding: 3px 0; word-break: break-all; color: #cbd5e1; }}
.link-ref {{ color: #fca5a5; font-weight: 600; }}
.link-erid {{ color: #86efac; font-weight: 600; }}

.text-box {{
    background: #0a0e17;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 15px 18px;
    max-height: 340px;
    overflow-y: auto;
    white-space: pre-wrap;
    font-size: 14px;
    color: #e2e8f0;
    margin-bottom: 14px;
}}

.actions-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(205px, 1fr));
    gap: 10px;
}}
.act-btn {{
    padding: 12px 13px;
    border-radius: 10px;
    border: 2px solid var(--border);
    background: var(--card2);
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    text-align: left;
}}
.act-btn:hover {{ border-color: #64748b; }}
.act-1.selected {{ background: rgba(239,68,68,0.22); border-color: var(--red); }}
.act-2.selected {{ background: rgba(59,130,246,0.22); border-color: var(--blue); }}
.act-3.selected {{ background: rgba(34,197,94,0.22); border-color: var(--green); }}
.act-4.selected {{ background: rgba(148,163,184,0.22); border-color: #94a3b8; }}
.hotkey {{
    display: inline-block;
    background: rgba(255,255,255,0.12);
    padding: 2px 7px;
    border-radius: 5px;
    margin-right: 6px;
    font-family: monospace;
    font-size: 12px;
}}
.act-sub {{ display: block; font-size: 11px; color: var(--muted); margin-top: 4px; font-weight: 400; }}

.footer-bar {{
    position: fixed;
    left: 0; right: 0; bottom: 0;
    background: rgba(11,15,25,0.96);
    border-top: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    z-index: 50;
}}
.btn-util {{
    padding: 8px 13px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--card2);
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
}}
.btn-save {{
    padding: 10px 16px;
    border-radius: 8px;
    border: none;
    background: var(--green);
    color: #052e16;
    font-size: 13px;
    font-weight: 800;
    cursor: pointer;
}}
.btn-golden {{
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--purple);
    background: rgba(168,85,247,0.18);
    color: #e9d5ff;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
}}
.help-panel {{
    margin-top: 16px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 18px;
    font-size: 13px;
    color: var(--muted);
}}
code {{
    background: #0a0e17;
    border: 1px solid var(--border);
    padding: 2px 6px;
    border-radius: 5px;
    color: #93c5fd;
    font-family: Consolas, monospace;
}}
</style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <h1>Пульт Микро-Вопросов v2 · Ансамблевая разметка VC.ru</h1>
            <div class="sub">Источник: <code>{src_display}</code> · В режиме микро-вопросов: <code>1</code> / <code>2</code> — ответ за 5 сек, <code>3</code> — весь текст, <code>G</code> — в экзамен Golden Set</div>
        </div>
        <div class="stats">
            <span class="pill pill-gold" id="stat-autogold">AUTO_GOLD (w=2.0): 0</span>
            <span class="pill pill-dispute" id="stat-disputed">Осталось вопросов: 0</span>
            <span class="pill" id="stat-human">Проверено вручную (w=3.0): 0</span>
            <label class="btn-util" style="cursor:pointer;">
                Загрузить JSONL
                <input type="file" id="jsonl-loader" accept=".jsonl,.json" style="display:none;">
            </label>
        </div>
    </div>

    <div class="tabs-bar">
        <div class="tab-group">
            <button type="button" class="tab-btn active" id="tab-micro" onclick="switchMode('micro')">
                ⚡ Точечные вопросы (только спорные)
            </button>
            <button type="button" class="tab-btn" id="tab-all" onclick="switchMode('all')">
                📋 Все статьи пула
            </button>
        </div>
        <div class="tab-group">
            <button type="button" class="btn-util" onclick="toggleImportDrawer()">
                📥 Вставить JSON из внешнего чата (пачка 10 шт.)
            </button>
        </div>
    </div>

    <div class="import-drawer" id="import-drawer">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>Импорт JSON-ответа внешнего арбитра (ChatGPT / Claude / Gemini / DeepSeek — по 10 карточек)</strong>
            <button type="button" class="btn-util" onclick="toggleImportDrawer()">Закрыть</button>
        </div>
        <div class="sub">Вставьте JSON-массив <code>[{{ "post_id": ..., "F1": ..., "regime": "...", "confidence": 0.95 }}]</code>. При совпадении с внутренним жюри карточка автоматически закроется в <code>AUTO_GOLD</code>.</div>
        <textarea class="import-textarea" id="chat-json-input" placeholder='[{{"post_id": 3259252, "F1": false, "F2": true, "F3": false, "F4": false, "F5": false, "regime": "CLEAN_NON_AD", "confidence": 0.96, "doubt_question_ru": ""}}]'></textarea>
        <div style="display:flex; gap:10px; align-items:center;">
            <button type="button" class="btn-save" onclick="applyExternalChatJson()">Сверить с жюри и применить</button>
            <span id="import-status" style="font-size:13px; color:#86efac;"></span>
        </div>
    </div>

    <div class="progress-wrap"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="nav-strip" id="nav-strip"></div>

    <div class="main-card" id="main-card">
        <div class="card-head">
            <div>
                <div class="sub" id="item-counter">Статья #1</div>
                <h2 class="post-title" id="post-title">Загрузка...</h2>
                <a class="orig-link" id="post-url" href="#" target="_blank" rel="noopener noreferrer"></a>
            </div>
            <div style="text-align:right; min-width:190px; display:flex; flex-direction:column; gap:6px; align-items:flex-end;">
                <span class="pill" id="current-label-pill">Не размечено</span>
                <label style="font-size:12px; color:#d8b4fe; cursor:pointer; user-select:none;">
                    <input type="checkbox" id="golden-candidate-chk" onchange="toggleGoldenCandidate(this.checked)">
                    [G] В независимый экзамен (Golden Set)
                </label>
            </div>
        </div>

        <div class="micro-box" id="micro-box" style="display:none;">
            <div class="jury-line" id="jury-line"></div>
            <div class="quote-snippet" id="quote-snippet"></div>
            <div class="micro-q-title" id="micro-q-title"></div>
            <div class="micro-btn-row" id="micro-btn-row"></div>
            <div style="margin-top:10px; display:flex; gap:10px; flex-wrap:wrap;">
                <button type="button" class="btn-util" onclick="toggleFullText()">[3] Показать / скрыть полный текст и ссылки</button>
                <button type="button" class="btn-util" onclick="setLabel('SKIP')">[4] Пропустить спорный кейс (SKIP)</button>
            </div>
        </div>

        <div id="full-context-wrap">
            <div class="meta-grid" id="meta-grid"></div>
            <div class="links-box" id="links-box"></div>
            <div class="text-box" id="text-box"></div>

            <div class="actions-grid">
                <button type="button" class="act-btn act-1" data-choice="UNMARKED_AD_NO_ERID" onclick="setLabel('UNMARKED_AD_NO_ERID')">
                    <span class="hotkey">A</span>Нарушение: Реклама/рефка без erid
                    <span class="act-sub">UNMARKED_AD_NO_ERID (промокод, UTM/ref, нативка)</span>
                </button>
                <button type="button" class="act-btn act-2" data-choice="CLEAN_MARKED_AD" onclick="setLabel('CLEAN_MARKED_AD')">
                    <span class="hotkey">M</span>Промаркировано (erid + Реклама)
                    <span class="act-sub">CLEAN_MARKED_AD (легальная интеграция с ОРД)</span>
                </button>
                <button type="button" class="act-btn act-3" data-choice="CLEAN_NON_AD" onclick="setLabel('CLEAN_NON_AD')">
                    <span class="hotkey">C</span>Не реклама (обзор 3+, блог, новость)
                    <span class="act-sub">CLEAN_NON_AD (аналитика, личный блог без оффера)</span>
                </button>
                <button type="button" class="act-btn act-4" data-choice="BANNED_OR_SPECIAL_REGIME" onclick="setLabel('BANNED_OR_SPECIAL_REGIME')">
                    <span class="hotkey">B</span>Запрещенный / Спецрежим (ст. 7 / 28)
                    <span class="act-sub">BANNED_OR_SPECIAL_REGIME (казино, VPN, крипто-рефка)</span>
                </button>
            </div>
        </div>
    </div>

    <div class="help-panel">
        <strong>Порядок работы для точности &gt;= 99%:</strong><br>
        1. Единогласные статьи (<code>4/4</code> моделей разных семейств, <code>conf &gt;= 0.90</code>) уже закрыты как <code>AUTO_GOLD</code> (вес <code>2.0</code>).<br>
        2. Ответьте клавишами <code>1</code> / <code>2</code> только на короткие точечные вопросы по спорным абзацам (вес <code>3.0</code>) или импортируйте пачку из внешнего чата.<br>
        3. Нажмите <strong>[💾 Скачать vcru_human_labeled.jsonl]</strong> (сохраните в <code>C:\\AdRadarData\\data\\vcru_human_labeled.jsonl</code>) и запустите переобучение:
        <code>.venv\\Scripts\\python.exe scripts\\build_champion_v3_bundle.py --retrain</code>.
    </div>
</div>

<div class="footer-bar">
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <button type="button" class="btn-util" onclick="goDelta(-1)">← Назад</button>
        <button type="button" class="btn-util" onclick="goDelta(1)">Вперед →</button>
        <button type="button" class="btn-util" onclick="jumpNextUnresolved()">К следующему вопросу</button>
        <button type="button" class="btn-util" onclick="clearSavedState()">Сбросить ответы браузера</button>
    </div>
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <span id="save-toast" style="font-size:13px; color:#86efac;"></span>
        <button type="button" class="btn-golden" onclick="downloadGoldenCandidatesJsonl()">🏆 Выгрузить в экзамен (Golden)</button>
        <button type="button" class="btn-save" onclick="downloadLabeledJsonl()">💾 Скачать vcru_human_labeled.jsonl (w=3.0 / 2.0)</button>
    </div>
</div>

<script id="vcru-dataset" type="application/json">{dataset_json}</script>
<script>
const REGIME_MAP = {regime_map};
const STORAGE_KEY = "adradar_vcru_labeler_v2";
const GOLDEN_STORAGE_KEY = "adradar_vcru_golden_flags_v2";
let records = JSON.parse(document.getElementById("vcru-dataset").textContent || "[]");
let labelsByPostId = {{}};
let goldenByPostId = {{}};
let viewMode = "micro";
let filteredIndices = [];
let posInFiltered = 0;
let showFullInMicro = false;

function hasEnsembleMeta() {{
    return records.some((r) => r.ensemble && r.ensemble.ensemble_status);
}}

function isAutoGold(r) {{
    return Boolean(r.ensemble && r.ensemble.ensemble_status === "AUTO_GOLD");
}}

function isDisputedUnanswered(r) {{
    if (labelsByPostId[r.post_id]) return false;
    if (isAutoGold(r)) return false;
    return true;
}}

function loadLocalProgress() {{
    try {{
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) labelsByPostId = JSON.parse(raw) || {{}};
        const gRaw = localStorage.getItem(GOLDEN_STORAGE_KEY);
        if (gRaw) goldenByPostId = JSON.parse(gRaw) || {{}};
    }} catch (e) {{}}
    records.forEach((r) => {{
        if (!labelsByPostId[r.post_id] && r.human_label) {{
            labelsByPostId[r.post_id] = r.human_label;
        }}
    }});
    if (!hasEnsembleMeta()) {{
        viewMode = "all";
    }}
}}

function persistLocalProgress() {{
    try {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify(labelsByPostId));
        localStorage.setItem(GOLDEN_STORAGE_KEY, JSON.stringify(goldenByPostId));
    }} catch (e) {{}}
}}

function rebuildFilteredIndices() {{
    filteredIndices = [];
    records.forEach((r, idx) => {{
        if (viewMode === "micro") {{
            if (!isAutoGold(r)) filteredIndices.push(idx);
        }} else {{
            filteredIndices.push(idx);
        }}
    }});
    if (posInFiltered >= filteredIndices.length) {{
        posInFiltered = Math.max(0, filteredIndices.length - 1);
    }}
}}

function switchMode(mode) {{
    viewMode = mode;
    posInFiltered = 0;
    showFullInMicro = (mode === "all");
    document.getElementById("tab-micro").classList.toggle("active", mode === "micro");
    document.getElementById("tab-all").classList.toggle("active", mode === "all");
    rebuildFilteredIndices();
    renderAll();
}}

function toggleImportDrawer() {{
    document.getElementById("import-drawer").classList.toggle("open");
}}

function toggleFullText() {{
    showFullInMicro = !showFullInMicro;
    renderCard();
}}

function toggleGoldenCandidate(checked) {{
    if (!filteredIndices.length) return;
    const r = records[filteredIndices[posInFiltered]];
    if (checked) goldenByPostId[r.post_id] = true;
    else delete goldenByPostId[r.post_id];
    persistLocalProgress();
    renderStats();
}}

function clearSavedState() {{
    if (!confirm("Очистить все ручные ответы в браузере?")) return;
    labelsByPostId = {{}};
    goldenByPostId = {{}};
    persistLocalProgress();
    renderAll();
}}

function esc(s) {{
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}}

function highlightQuoteHtml(quote, trigger) {{
    const safeQuote = esc(quote || "");
    const trig = String(trigger || "").trim();
    if (!trig) return safeQuote;
    const safeTrig = esc(trig);
    const idx = safeQuote.toLowerCase().indexOf(safeTrig.toLowerCase());
    if (idx === -1) return safeQuote;
    const before = safeQuote.slice(0, idx);
    const match = safeQuote.slice(idx, idx + safeTrig.length);
    const after = safeQuote.slice(idx + safeTrig.length);
    return before + '<span class="hl-trig">' + match + '</span>' + after;
}}

function renderStats() {{
    let cntAuto = 0, cntHuman = 0, cntPending = 0, cntGolden = 0;
    records.forEach((r) => {{
        const h = labelsByPostId[r.post_id];
        if (h && h !== "SKIP") {{
            cntHuman++;
            if (goldenByPostId[r.post_id]) cntGolden++;
        }} else if (!h && isAutoGold(r)) {{
            cntAuto++;
        }} else if (!h) {{
            cntPending++;
        }}
    }});
    document.getElementById("stat-autogold").textContent = "AUTO_GOLD (w=2.0): " + cntAuto;
    document.getElementById("stat-disputed").textContent = "Осталось вопросов: " + cntPending;
    document.getElementById("stat-human").textContent =
        "Вручную (w=3.0): " + cntHuman + (cntGolden ? (" · в Golden: " + cntGolden) : "");
    const resolved = cntAuto + cntHuman;
    const pct = records.length > 0 ? Math.round((resolved * 100) / records.length) : 0;
    document.getElementById("progress-fill").style.width = pct + "%";
}}

function renderNav() {{
    const strip = document.getElementById("nav-strip");
    strip.innerHTML = "";
    filteredIndices.forEach((recIdx, fPos) => {{
        const r = records[recIdx];
        const btn = document.createElement("button");
        btn.type = "button";
        const lbl = labelsByPostId[r.post_id];
        let cls = "nav-dot" + (fPos === posInFiltered ? " active" : "");
        if (lbl) cls += " lbl-" + lbl;
        else if (isAutoGold(r)) cls += " auto-gold";
        btn.className = cls;
        btn.textContent = String(fPos + 1);
        btn.title = "#" + r.post_id + " " + (r.title || "");
        btn.onclick = () => {{ posInFiltered = fPos; renderAll(); }};
        strip.appendChild(btn);
    }});
}}

function renderCard() {{
    if (!filteredIndices.length) {{
        document.getElementById("post-title").textContent =
            viewMode === "micro"
                ? "Все статьи единогласно решены ансамблем (0 спорных вопросов)!"
                : "Нет загруженных статей.";
        document.getElementById("micro-box").style.display = "none";
        document.getElementById("full-context-wrap").style.display = "none";
        return;
    }}

    const r = records[filteredIndices[posInFiltered]];
    const lbl = labelsByPostId[r.post_id] || "";
    const ens = r.ensemble || {{}};
    const mq = ens.micro_question || null;

    document.getElementById("item-counter").textContent =
        (viewMode === "micro" ? "Спорная карточка " : "Статья ") +
        (posInFiltered + 1) + " из " + filteredIndices.length +
        " · post_id=" + r.post_id + (r.rubric ? (" · рубрика: " + r.rubric) : "");
    document.getElementById("post-title").textContent = r.title || ("[Пост #" + r.post_id + "]");

    const urlEl = document.getElementById("post-url");
    urlEl.textContent = r.url || "";
    urlEl.href = r.url || "#";

    const pill = document.getElementById("current-label-pill");
    if (lbl) {{
        pill.textContent = "HUMAN: " + lbl;
    }} else if (isAutoGold(r)) {{
        pill.textContent = "AUTO_GOLD: " + (ens.consensus_regime || "");
    }} else {{
        pill.textContent = "Ждет ответа (5 сек)";
    }}

    document.getElementById("golden-candidate-chk").checked = Boolean(goldenByPostId[r.post_id]);

    const microBox = document.getElementById("micro-box");
    const fullWrap = document.getElementById("full-context-wrap");

    if (mq && (viewMode === "micro" || !isAutoGold(r))) {{
        microBox.style.display = "block";
        document.getElementById("jury-line").innerHTML =
            "<strong>Голоса жюри:</strong> " + esc(mq.jury_line_ru || "");
        document.getElementById("quote-snippet").innerHTML =
            highlightQuoteHtml(mq.quote_snippet || "", mq.highlight_trigger || "");
        document.getElementById("micro-q-title").textContent = mq.question_ru || "";

        const opt1 = mq.option_1 || {{ label_ru: "Вариант 1", resulting_regime: "CLEAN_NON_AD" }};
        const opt2 = mq.option_2 || {{ label_ru: "Вариант 2", resulting_regime: "UNMARKED_AD_NO_ERID" }};
        document.getElementById("micro-btn-row").innerHTML =
            '<button type="button" class="micro-btn opt1' + (lbl === opt1.resulting_regime ? ' selected' : '') + '" ' +
            'onclick="setLabel(\\'' + esc(opt1.resulting_regime) + '\\')">' +
            '<span class="hotkey">1</span>' + esc(opt1.label_ru) +
            '<span class="act-sub">Вычислит режим → ' + esc(opt1.resulting_regime) + '</span></button>' +
            '<button type="button" class="micro-btn opt2' + (lbl === opt2.resulting_regime ? ' selected' : '') + '" ' +
            'onclick="setLabel(\\'' + esc(opt2.resulting_regime) + '\\')">' +
            '<span class="hotkey">2</span>' + esc(opt2.label_ru) +
            '<span class="act-sub">Вычислит режим → ' + esc(opt2.resulting_regime) + '</span></button>';

        fullWrap.style.display = (viewMode === "all" || showFullInMicro) ? "block" : "none";
    }} else {{
        microBox.style.display = "none";
        fullWrap.style.display = "block";
    }}

    const mv = r.model_verdict || {{}};
    const erids = r.erids || [];
    const eridText = erids.length ? erids.join(", ") : "Нет";
    document.getElementById("meta-grid").innerHTML = [
        '<div class="meta-box"><div class="meta-lbl">Площадка VC.ru</div>' +
            'isPromo: <span class="' + (r.is_promo ? 'badge-warn' : '') + '">' + Boolean(r.is_promo) + '</span> · ' +
            'isEditorial: <span class="' + (r.is_editorial ? 'badge-ok' : '') + '">' + Boolean(r.is_editorial) + '</span></div>',
        '<div class="meta-box"><div class="meta-lbl">Маркировка Tier-0</div>' +
            'erid: <span class="' + (erids.length ? 'badge-ok' : 'badge-warn') + '">' + esc(eridText) + '</span><br>' +
            'слово «Реклама»: <strong>' + Boolean(r.has_strict_ad_label) + '</strong></div>',
        '<div class="meta-box"><div class="meta-lbl">Локальное ядро SheafNet</div>' +
            'режим: <code>' + esc(mv.regime || "N/A") + '</code> · p=' +
            (mv.violation_probability != null ? mv.violation_probability : "N/A") + '</div>'
    ].join("");

    const links = r.external_links || [];
    const targets = r.commercial_targets || [];
    const hiddenContacts = targets.filter((t) => !/^https?:\\/\\//i.test(t));
    const linksBox = document.getElementById("links-box");
    if (!links.length && !hiddenContacts.length) {{
        linksBox.innerHTML = '<div class="meta-lbl">Внешние ссылки и контакты</div><span style="color:#94a3b8;">Не найдены (0 внешних ссылок)</span>';
    }} else {{
        const itemsHtml = links.map((lnk) => {{
            const isRef = /([?&](?:ref|pid|aff|promo|r)=|utm_medium=affiliate|\\/(?:r|promo|aff)\\/)/i.test(lnk);
            const hasErid = /erid=/i.test(lnk);
            const cls = hasErid ? "link-item link-erid" : (isRef ? "link-item link-ref" : "link-item");
            const tag = hasErid ? "[erid] " : (isRef ? "[ref/utm] " : "");
            return '<a class="' + cls + '" href="' + esc(lnk) + '" target="_blank" rel="noopener noreferrer">' + esc(tag + lnk) + '</a>';
        }}).join("");
        const contactsHtml = hiddenContacts.length
            ? '<div style="margin-top:6px; padding-top:6px; border-top:1px dashed var(--border);">' +
              '<span class="badge-warn">[Контакты/домены/боты]: </span>' +
              hiddenContacts.map((c) => '<code style="margin-right:8px;">' + esc(c) + '</code>').join("") +
              '</div>'
            : '';
        linksBox.innerHTML = '<div class="meta-lbl">Внешние ссылки (' + links.length + ') и контакты (' + hiddenContacts.length + ')</div>' + itemsHtml + contactsHtml;
    }}

    document.getElementById("text-box").textContent = r.text_window || r.full_text || "";

    document.querySelectorAll(".act-btn").forEach((b) => {{
        if (b.getAttribute("data-choice") === lbl) b.classList.add("selected");
        else b.classList.remove("selected");
    }});
}}

function renderAll() {{
    rebuildFilteredIndices();
    renderStats();
    renderNav();
    renderCard();
}}

function setLabel(choice) {{
    if (!filteredIndices.length) return;
    const r = records[filteredIndices[posInFiltered]];
    labelsByPostId[r.post_id] = choice;
    persistLocalProgress();
    showFullInMicro = (viewMode === "all");
    if (posInFiltered < filteredIndices.length - 1) {{
        posInFiltered++;
    }}
    renderAll();
}}

function goDelta(delta) {{
    if (!filteredIndices.length) return;
    posInFiltered = Math.max(0, Math.min(filteredIndices.length - 1, posInFiltered + delta));
    renderAll();
}}

function jumpNextUnresolved() {{
    for (let i = 0; i < filteredIndices.length; i++) {{
        const cand = (posInFiltered + 1 + i) % filteredIndices.length;
        const r = records[filteredIndices[cand]];
        if (!labelsByPostId[r.post_id] && !isAutoGold(r)) {{
            posInFiltered = cand;
            renderAll();
            return;
        }}
    }}
    document.getElementById("save-toast").textContent = "Все спорные вопросы уже решены!";
}}

function applyExternalChatJson() {{
    const raw = document.getElementById("chat-json-input").value.trim();
    if (!raw) return;
    let arr = null;
    try {{
        const cleaned = raw.replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
        const parsed = JSON.parse(cleaned);
        arr = Array.isArray(parsed) ? parsed : (parsed.items || []);
    }} catch (e) {{
        document.getElementById("import-status").textContent = "Ошибка JSON: " + e.message;
        return;
    }}

    const byId = {{}};
    arr.forEach((x) => {{ if (x && x.post_id != null) byId[Number(x.post_id)] = x; }});

    let matched = 0, promoted = 0, updatedQ = 0;
    records.forEach((r) => {{
        const item = byId[Number(r.post_id)];
        if (!item) return;
        matched++;
        const extReg = String(item.regime || "").trim();
        if (!REGIME_MAP[extReg] || extReg === "SKIP") return;
        const extConf = Number(item.confidence != null ? item.confidence : 0.95);

        const ens = r.ensemble || {{ votes: [] }};
        const priorVotes = (ens.votes || []).filter((v) => v.family !== "external_frontier_chat");
        priorVotes.push({{
            voter: "External-Chat",
            family: "external_frontier_chat",
            regime: extReg,
            is_violation: Boolean(REGIME_MAP[extReg].expected_is_violation),
            confidence: extConf
        }});
        ens.votes = priorVotes;

        const regimes = new Set(priorVotes.map((v) => v.regime));
        const extFamilies = new Set(
            priorVotes
                .filter((v) => v.family !== "cellular_sheaf_local" && v.family !== "vega_nli_local")
                .map((v) => v.family)
        );
        const avgConf = priorVotes.reduce((acc, v) => acc + Number(v.confidence || 0.9), 0) / Math.max(1, priorVotes.length);

        if (priorVotes.length >= 4 && regimes.size === 1 && extFamilies.size >= 2 && avgConf >= 0.90) {{
            ens.ensemble_status = "AUTO_GOLD";
            ens.consensus_regime = extReg;
            ens.consensus_is_violation = Boolean(REGIME_MAP[extReg].expected_is_violation);
            ens.sample_weight = 2.0;
            promoted++;
        }} else {{
            ens.ensemble_status = "NEEDS_MICRO_Q";
            if (ens.micro_question && item.doubt_question_ru) {{
                ens.micro_question.question_ru = "Вопрос арбитра: " + String(item.doubt_question_ru);
            }}
            updatedQ++;
        }}
        r.ensemble = ens;
    }});

    document.getElementById("import-status").textContent =
        "Сопоставлено: " + matched + " · закрыто в AUTO_GOLD: " + promoted + " · осталось вопросов: " + updatedQ;
    renderAll();
}}

function downloadLabeledJsonl() {{
    const outLines = [];
    let cntHuman = 0, cntAuto = 0, skippedGolden = 0;

    records.forEach((r) => {{
        const hChoice = labelsByPostId[r.post_id];
        let regime = "";
        let weight = 3.0;
        let source = "HUMAN_VERIFIED";

        if (hChoice) {{
            if (hChoice === "SKIP") return;
            if (goldenByPostId[r.post_id]) {{
                skippedGolden++;
                return;
            }}
            regime = hChoice;
            weight = 3.0;
            source = "HUMAN_VERIFIED";
            cntHuman++;
        }} else if (isAutoGold(r)) {{
            regime = r.ensemble.consensus_regime;
            weight = 2.0;
            source = "UNANIMOUS_CONSENSUS";
            cntAuto++;
        }} else {{
            return;
        }}

        const meta = REGIME_MAP[regime];
        if (!meta || meta.expected_is_violation === null) return;

        outLines.push(JSON.stringify({{
            post_id: r.post_id,
            url: r.url,
            rubric: r.rubric || "general",
            title: r.title,
            author_domain: "vc.ru",
            text: r.full_text || r.text_window,
            text_window: r.text_window,
            external_links: r.external_links || [],
            erids: r.erids || [],
            is_promo: Boolean(r.is_promo),
            platform_ad_meta: r.platform_ad_meta || {{}},
            human_label: regime,
            label_source: source,
            sample_weight: weight,
            expected_is_violation: meta.expected_is_violation,
            expected_regime: meta.expected_regime,
            expected_regime_id: meta.expected_regime_id,
            labeled_at: new Date().toISOString()
        }}));
    }});

    if (!outLines.length) {{
        alert("Нет подтвержденных карточек (HUMAN_VERIFIED или AUTO_GOLD) для экспорта.");
        return;
    }}

    const blob = new Blob([outLines.join("\\n") + "\\n"], {{ type: "application/x-ndjson;charset=utf-8" }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "vcru_human_labeled.jsonl";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    document.getElementById("save-toast").textContent =
        "Сохранено " + outLines.length + " (HUMAN w=3: " + cntHuman + ", AUTO w=2: " + cntAuto +
        (skippedGolden ? (", отложено в Golden: " + skippedGolden) : "") + ")";
}}

function downloadGoldenCandidatesJsonl() {{
    const outLines = [];
    records.forEach((r) => {{
        const hChoice = labelsByPostId[r.post_id];
        if (!hChoice || hChoice === "SKIP" || !goldenByPostId[r.post_id]) return;
        const meta = REGIME_MAP[hChoice];
        if (!meta) return;
        outLines.push(JSON.stringify({{
            post_id: r.post_id,
            url: r.url,
            title: r.title,
            author_domain: "vc.ru",
            text: r.full_text || r.text_window,
            external_links: r.external_links || [],
            is_promo_platform_flag: Boolean(r.is_promo),
            erids: r.erids || [],
            platform_ad_meta: r.platform_ad_meta || {{}},
            expected_is_violation: meta.expected_is_violation,
            expected_regime: meta.expected_regime,
            expected_regime_id: meta.expected_regime_id,
            label_basis: "human_verified_micro_q_golden_expansion",
            collected_at: new Date().toISOString()
        }}));
    }});
    if (!outLines.length) {{
        alert("Отметьте хотя бы одну проверенную вручную карточку галочкой [G] В независимый экзамен.");
        return;
    }}
    const blob = new Blob([outLines.join("\\n") + "\\n"], {{ type: "application/x-ndjson;charset=utf-8" }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "golden_candidates_vcru.jsonl";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    document.getElementById("save-toast").textContent =
        "Выгружено кандидатов в Golden Set: " + outLines.length;
}}

document.getElementById("jsonl-loader").addEventListener("change", (ev) => {{
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {{
        const lines = String(e.target.result || "").split(/\\r?\\n/).filter((l) => l.trim());
        const parsed = [];
        lines.forEach((l) => {{
            try {{ parsed.push(JSON.parse(l)); }} catch (_) {{}}
        }});
        if (parsed.length) {{
            records = parsed;
            posInFiltered = 0;
            loadLocalProgress();
            renderAll();
            document.getElementById("save-toast").textContent = "Загружено " + parsed.length + " статей.";
        }}
    }};
    reader.readAsText(file, "utf-8");
}});

document.addEventListener("keydown", (e) => {{
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
    if (!filteredIndices.length) return;
    const r = records[filteredIndices[posInFiltered]];
    const mq = (r.ensemble && r.ensemble.micro_question) ? r.ensemble.micro_question : null;

    if (mq && (viewMode === "micro" || !isAutoGold(r))) {{
        if (e.key === "1" && mq.option_1) {{ setLabel(mq.option_1.resulting_regime); return; }}
        if (e.key === "2" && mq.option_2) {{ setLabel(mq.option_2.resulting_regime); return; }}
        if (e.key === "3") {{ toggleFullText(); return; }}
        if (e.key === "4") {{ setLabel("SKIP"); return; }}
    }} else {{
        if (e.key === "1") {{ setLabel("UNMARKED_AD_NO_ERID"); return; }}
        if (e.key === "2") {{ setLabel("CLEAN_MARKED_AD"); return; }}
        if (e.key === "3") {{ setLabel("CLEAN_NON_AD"); return; }}
        if (e.key === "4") {{ setLabel("SKIP"); return; }}
    }}

    if (e.key === "g" || e.key === "G" || e.key === "п" || e.key === "П") {{
        const chk = document.getElementById("golden-candidate-chk");
        chk.checked = !chk.checked;
        toggleGoldenCandidate(chk.checked);
    }} else if (e.key === "ArrowLeft") {{
        goDelta(-1);
    }} else if (e.key === "ArrowRight") {{
        goDelta(1);
    }}
}});

loadLocalProgress();
renderAll();
</script>
</body>
</html>
"""


def generate_labeler_page(
    input_jsonl: Path | None = None,
    output_html: Path | None = None,
) -> Path:
    data_dir = _active_data_dir() / "data"
    if input_jsonl is not None:
        in_path = input_jsonl
    elif (data_dir / "vcru_ensemble_pool.jsonl").exists():
        in_path = data_dir / "vcru_ensemble_pool.jsonl"
    else:
        in_path = data_dir / "vcru_to_label.jsonl"

    out_path = output_html or (data_dir / "labeler.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    if in_path.exists():
        for raw_line in in_path.read_text(encoding="utf-8").splitlines():
            if raw_line.strip():
                records.append(json.loads(raw_line))

    html_content = build_labeler_html(records, in_path)
    out_path.write_text(html_content, encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate offline HTML Micro-Question Desk v2 for VC.ru articles")
    parser.add_argument("--input", type=str, default=None, help="Path to vcru_ensemble_pool.jsonl or vcru_to_label.jsonl")
    parser.add_argument("--output", type=str, default=None, help="Path to output labeler.html")
    args = parser.parse_args()

    in_path = Path(args.input) if args.input else None
    out_path = Path(args.output) if args.output else None
    res = generate_labeler_page(input_jsonl=in_path, output_html=out_path)
    print(f"[+] Offline VC.ru Micro-Question Desk v2 ready: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
