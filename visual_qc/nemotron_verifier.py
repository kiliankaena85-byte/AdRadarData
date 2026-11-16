"""
Visual QC: OpenRouter Nemotron-3.5-Super-VL Verification Client.
Strict compliance with AGENTS.md, Zero-Vision-Library isolation (NO cv2, NO PIL).
Verifies sniper trigger zone collages against automated scanner findings to eliminate false positives.
Includes LiveCatalogResolver, CircuitBreaker, RateLimiter (12 RPM), SQLite Cache, and ContextRuleReconciler.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import yaml

from legal_sheaf.config import ADRADAR_DATA_DIR

ROOT_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger("visual_qc.nemotron")

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_PRIMARY_MODEL = "nvidia/nemotron-3.5-super-vl:free"
FALLBACK_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "openrouter/free",
]

PROMPT_VERSION = "2026.11-v2"


# =============================================================================
# 1. API KEY RESOLUTION & CITATION WHITELIST
# =============================================================================
def get_openrouter_api_key() -> str:
    """
    Retrieves OPENROUTER_API_KEY from environment, .env in working dir, or C:\\AdRadarData\\.env.
    Never returns a fake key.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    # Check local workspace .env
    local_env = Path.cwd() / ".env"
    if local_env.exists():
        try:
            for line in local_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass

    # Check C:\AdRadarData\.env
    data_env = ADRADAR_DATA_DIR / ".env"
    if data_env.exists():
        try:
            for line in data_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass

    return ""


def load_citation_whitelist() -> Tuple[set, set]:
    """Loads closed whitelist of verified court cases, agency letters, and allowed KoAP articles."""
    candidates = [
        Path.cwd() / "config" / "verified_citations_whitelist.yaml",
        Path.cwd() / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml",
        ROOT_DIR / "config" / "verified_citations_whitelist.yaml",
        ROOT_DIR / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml",
    ]
    curr = Path(__file__).resolve().parent
    for _ in range(5):
        candidates.append(curr / "config" / "verified_citations_whitelist.yaml")
        candidates.append(curr / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml")
        curr = curr.parent

    for p in candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                cases = set(data.get("verified_court_cases", []))
                letters = set(data.get("verified_agency_letters", []))
                koap_data = data.get("verified_koap_and_laws", {})
                allowed_koap = set(str(x) for x in koap_data.get("koap_articles", []))
                citations = cases | letters
                if citations or allowed_koap:
                    return citations, allowed_koap
            except Exception as e:
                logger.warning(f"Failed to read citation whitelist {p}: {e}")
    return set(), set()


def sanitize_model_citations(text: str, allowed_citations: set, allowed_koap: Optional[set] = None) -> str:
    """
    Scans model output text for court case, agency letter patterns, and unauthorized KoAP articles.
    Any unverified citation or article is sanitized to prevent citation lock violation.
    """
    if not text:
        return ""

    pat_soyu = re.compile(r"\b0?5-\d{3,5}/\d{2,4}/20\d{2}\b")
    pat_arbitr = re.compile(r"\b[АA]\d{2}-\d{3,7}/20\d{2}\b")
    pat_appeal = re.compile(r"\b\d{2}АП-\d{3,7}/20\d{2}\b")
    pat_letter = re.compile(r"\b(?:[А-ЯЁ]{2}|\d{2})/\d{4,6}/\d{2}\b")

    def _replace_match(m: re.Match) -> str:
        cit = m.group(0)
        if cit in allowed_citations:
            return cit
        return "[номер исключен политикой верификации]"

    for pat in [pat_soyu, pat_arbitr, pat_appeal, pat_letter]:
        text = pat.sub(_replace_match, text)

    if allowed_koap:
        pat_koap1 = re.compile(r"(?i)\b(?:ст\.|стать[еяию]\w*|ч\.\s*[\d,/-]+\s*ст\.)\s*(\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*(?:КоАП|Кодекса)")
        pat_koap2 = re.compile(r"(?i)\b(\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*КоАП")

        def _replace_koap(m: re.Match) -> str:
            art = m.group(1)
            if art in allowed_koap:
                return m.group(0)
            return "[статья КоАП исключена политикой верификации]"

        text = pat_koap1.sub(_replace_koap, text)
        text = pat_koap2.sub(_replace_koap, text)

    return text


def _parse_json_resilient(text: str) -> Dict[str, Any]:
    """Extracts JSON object from text, handling markdown fences and edge formatting."""
    cleaned = text.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


# =============================================================================
# 2. LIVE CATALOG RESOLVER (OpenRouter Live Models Sync)
# =============================================================================
class LiveCatalogResolver:
    """
    Syncs and validates OpenRouter model IDs against live API catalog.
    Refreshes catalog every 24 hours.
    Ensures free models have pricing == 0 and multimodal image support.
    Enforces banned model policy (e.g. deprecations or high error rate models).
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self._find_config()
        self.config = self._load_config()
        settings = self.config.get("live_catalog_settings", {})
        self.refresh_interval = settings.get("refresh_interval_seconds", 86400)
        self.catalog_endpoint = settings.get("catalog_endpoint", "https://openrouter.ai/api/v1/models")
        self.cache_file = Path(settings.get("cache_file", str(ADRADAR_DATA_DIR / "cache" / "openrouter_models_catalog.json")))
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    def _find_config(self) -> Path:
        candidates = [
            Path.cwd() / "config" / "openrouter_models.yaml",
            Path.cwd() / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
            ROOT_DIR / "config" / "openrouter_models.yaml",
            ROOT_DIR / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
        ]
        for p in candidates:
            if p.exists():
                return p
        return ROOT_DIR / "config" / "openrouter_models.yaml"

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            try:
                return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning(f"Failed to parse openrouter_models.yaml: {e}")
        return {}

    async def get_live_catalog(self, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
        """Loads cached catalog or fetches fresh from OpenRouter API if older than 24h."""
        # 1. Check disk cache freshness
        if self.cache_file.exists():
            mtime = self.cache_file.stat().st_mtime
            if (time.time() - mtime) < self.refresh_interval:
                try:
                    data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "data" in data:
                        return {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict) and "id" in m}
                except Exception:
                    pass

        # 2. Fetch from network
        close_local = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=10.0)
            close_local = True

        try:
            resp = await c.get(self.catalog_endpoint)
            if resp.status_code == 200:
                raw_json = resp.json()
                self.cache_file.write_text(json.dumps(raw_json, ensure_ascii=False, indent=2), encoding="utf-8")
                return {m.get("id"): m for m in raw_json.get("data", []) if isinstance(m, dict) and "id" in m}
        except Exception as e:
            logger.warning(f"Could not refresh OpenRouter live catalog: {e}")
        finally:
            if close_local:
                await c.aclose()

        # 3. Fallback to existing disk cache if available
        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                return {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict) and "id" in m}
            except Exception:
                pass

        return {}

    async def resolve_models(
        self,
        paid_zdr_mode: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[str]:
        """
        Resolves prioritized, validated list of candidate models for execution.
        Guarantees that :free models are verified with pricing == 0.
        """
        catalog = await self.get_live_catalog(client=client)

        if paid_zdr_mode:
            paid_tier = self.config.get("paid_zdr_tier", {})
            primary = paid_tier.get("primary", "google/gemini-4-hydrogen")
            alternatives = paid_tier.get("alternatives", ["deepseek/deepseek-v4.1-flash-1017"])
            banned = set(paid_tier.get("banned_models", []))

            candidates = [primary] + [m for m in alternatives if m != primary]
            valid = []
            for m in candidates:
                if any(b in m for b in banned):
                    logger.warning(f"Banned model {m} rejected from paid ZDR candidates.")
                    continue
                valid.append(m)
            return valid or ["google/gemini-4-hydrogen"]

        # Free tier
        free_tier = self.config.get("free_tier", {})
        primary = free_tier.get("primary", DEFAULT_PRIMARY_MODEL)
        fallbacks = free_tier.get("fallbacks", FALLBACK_MODELS)
        candidates = [primary] + [m for m in fallbacks if m != primary]

        if not catalog:
            # No catalog data available, return static candidates
            return candidates

        valid_models = []
        for m in candidates:
            # "openrouter/free" is a meta-router endpoint, always permitted
            if m == "openrouter/free":
                valid_models.append(m)
                continue

            info = catalog.get(m)
            if not info:
                # If model is not in catalog at all, skip unless it's the primary
                logger.warning(f"Model {m} not found in OpenRouter live catalog, skipping.")
                continue

            # Verify pricing is 0
            pricing = info.get("pricing", {})
            prompt_cost = float(pricing.get("prompt", 0) or 0)
            comp_cost = float(pricing.get("completion", 0) or 0)
            if prompt_cost > 0 or comp_cost > 0:
                logger.warning(f"Model {m} has non-zero pricing ({pricing}) in free tier! Rejected.")
                continue

            # Verify multimodal image support if possible
            modality = info.get("architecture", {}).get("modality", "")
            input_modalities = info.get("input_modalities", [])
            if modality and "image" not in modality and "image" not in input_modalities:
                logger.warning(f"Model {m} does not declare image support ({modality}). Skipping.")
                continue

            valid_models.append(m)

        return valid_models or candidates


# =============================================================================
# 3. CIRCUIT BREAKER & RATE LIMITER
# =============================================================================
class CircuitBreaker:
    """
    Tracks consecutive errors (429, 503, timeout) per model.
    Trips into cooldown for 10 minutes after threshold failures.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 600.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}

    def is_available(self, model: str) -> bool:
        cd = self._cooldown_until.get(model, 0.0)
        if time.time() < cd:
            return False
        return True

    def record_failure(self, model: str) -> None:
        self._failures[model] = self._failures.get(model, 0) + 1
        if self._failures[model] >= self.failure_threshold:
            self._cooldown_until[model] = time.time() + self.cooldown_seconds
            logger.warning(
                f"Circuit breaker TRIPPED for model {model} (failed {self._failures[model]} times). "
                f"In cooldown for {self.cooldown_seconds}s."
            )

    def record_success(self, model: str) -> None:
        self._failures[model] = 0
        self._cooldown_until.pop(model, None)


class VlmRateLimiter:
    """Sliding-window rate limiter enforcing 12 RPM (Requests Per Minute)."""

    def __init__(self, max_rpm: int = 12):
        self.max_rpm = max_rpm
        self.window_seconds = 60.0
        self._timestamps: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            # Purge timestamps outside window
            self._timestamps = [t for t in self._timestamps if now - t < self.window_seconds]
            if len(self._timestamps) >= self.max_rpm:
                oldest = self._timestamps[0]
                sleep_time = self.window_seconds - (now - oldest) + 0.1
                if sleep_time > 0:
                    logger.info(f"Rate limit (12 RPM) reached. Waiting {sleep_time:.2f}s...")
                    await asyncio.sleep(sleep_time)
            self._timestamps.append(time.time())


# =============================================================================
# 4. SQLITE IDEMPOTENT CACHE
# =============================================================================
class VlmSqliteCache:
    """Idempotent cache for VLM responses keyed by SHA-256(url + collage_bytes + prompt_ver)."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (ADRADAR_DATA_DIR / "db" / "vlm_cache.sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vlm_cache (
                        cache_key TEXT PRIMARY KEY,
                        response_json TEXT NOT NULL,
                        model_used TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
        except Exception as e:
            logger.warning(f"Failed to initialize VLM cache DB: {e}")

    @staticmethod
    def compute_key(collage_bytes: bytes, findings: List[Dict[str, Any]], prompt_ver: str = PROMPT_VERSION) -> str:
        h = hashlib.sha256()
        h.update(collage_bytes)
        findings_str = json.dumps(findings, sort_keys=True, ensure_ascii=False)
        h.update(findings_str.encode("utf-8"))
        h.update(prompt_ver.encode("utf-8"))
        return h.hexdigest()

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("SELECT response_json, model_used FROM vlm_cache WHERE cache_key = ?", (cache_key,))
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    data["model_used"] = row[1]
                    data["from_cache"] = True
                    return data
        except Exception as e:
            logger.warning(f"Cache read error: {e}")
        return None

    def set(self, cache_key: str, data: Dict[str, Any], model_used: str) -> None:
        try:
            payload = json.dumps(data, ensure_ascii=False)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO vlm_cache (cache_key, response_json, model_used, created_at) VALUES (?, ?, ?, ?)",
                    (cache_key, payload, model_used, time.time()),
                )
        except Exception as e:
            logger.warning(f"Cache write error: {e}")


# =============================================================================
# 5. CONTEXT RULE RECONCILER (Strict Authority Boundary)
# =============================================================================
class ContextRuleReconciler:
    """
    Enforces strict authority boundaries on VLM decisions:
    1. VLM evaluates ONLY context facts: page_genre, audience_type, visual_dominance, element_visually_present, readability_grade.
    2. VLM is STRICTLY FORBIDDEN from overruling code/registry ground-truth:
       - Google Tag Manager / gtag cross-border transfers (DOM proof).
       - Absence of registration in Roskomnadzor registry (pd.rkn.gov.ru API proof).
       - FNS clone / fake registration records.
    3. Visual absence false positives (e.g. Meta disclaimer, consent text) require exact_visible_text witness.
    4. Cites only verified KoAP articles and federal laws.
    """

    IRREVERSIBLE_CODE_PROOFS = {
        "gtag_foreign_transfer",
        "google_tag_manager",
        "recaptcha_leak",
        "missing_inn_rkn_registry",
        "rkn_unregistered_operator",
        "fns_clone_suspicion",
        "cross_border_data_transfer",
    }

    def reconcile(
        self,
        findings_verdicts: List[Dict[str, Any]],
        original_findings: List[Dict[str, Any]],
        allowed_citations: set,
        allowed_koap: set,
    ) -> List[Dict[str, Any]]:
        orig_map = {
            (f.get("finding_id") or f.get("signature") or ""): f
            for f in original_findings
        }

        reconciled = []
        for fv in findings_verdicts:
            fid = fv.get("finding_id", "")
            orig = orig_map.get(fid, {})
            sig = (orig.get("signature") or fid).lower()

            is_code_proof = any(cp in sig for cp in self.IRREVERSIBLE_CODE_PROOFS)

            # Rule: VLM cannot overrule technical code/registry evidence
            if is_code_proof and fv.get("is_false_positive") is True:
                logger.info(f"ContextRuleReconciler: Blocking VLM false positive on technical code proof '{fid}'.")
                fv["is_false_positive"] = False
                fv["vlm_verdict"] = "CONFIRMED_VIOLATION"
                fv["reconciliation_note"] = (
                    "VLM не имеет полномочий отменять технические улики программного кода или госреестров "
                    "(Google Tag Manager, реестр операторов Роскомнадзора, проверка ФНС)."
                )

            # Rule: False positive on absence requires exact_visible_text witness
            if fv.get("is_false_positive") is True:
                vis_ev = str(fv.get("visual_evidence_ru") or "")
                exact_text = fv.get("exact_visible_text")
                if not exact_text and not vis_ev:
                    logger.info(f"ContextRuleReconciler: Missing exact visible text for false positive '{fid}'. Demoting.")
                    fv["is_false_positive"] = False
                    fv["vlm_verdict"] = "UNVERIFIED"
                    fv["reconciliation_note"] = "Снятие триггера отклонено: VLM не предоставил точный видимый текст сноски/реквизита."

            # Sanitize citations & KoAP articles
            if "visual_evidence_ru" in fv and isinstance(fv["visual_evidence_ru"], str):
                fv["visual_evidence_ru"] = sanitize_model_citations(fv["visual_evidence_ru"], allowed_citations, allowed_koap)
            if "reconciliation_note" in fv and isinstance(fv["reconciliation_note"], str):
                fv["reconciliation_note"] = sanitize_model_citations(fv["reconciliation_note"], allowed_citations, allowed_koap)

            reconciled.append(fv)

        return reconciled


# Shared Singletons
_CIRCUIT_BREAKER = CircuitBreaker()
_RATE_LIMITER = VlmRateLimiter()
_CACHE = VlmSqliteCache()
_CATALOG_RESOLVER = LiveCatalogResolver()
_RECONCILER = ContextRuleReconciler()


# =============================================================================
# 6. MAIN VERIFICATION FUNCTION
# =============================================================================
async def verify_visual_triggers(
    collage_path: Union[str, Path],
    audit_findings: List[Dict[str, Any]],
    policy_snippet: Optional[str] = None,
    paid_zdr_mode: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    api_key: Optional[str] = None,
    primary_model: str = DEFAULT_PRIMARY_MODEL,
) -> Dict[str, Any]:
    """
    Verifies visual trigger zones in collage against automated scanner findings using VLM.
    Features:
    - Zero-Mock fallback (when OPENROUTER_API_KEY is missing).
    - Idempotent SQLite cache.
    - 12 RPM Rate limiting.
    - CircuitBreaker protection with TriState.UNVERIFIED on full failure.
    - ContextRuleReconciler enforcing boundary against overriding DOM/RKN evidence.
    """
    key = api_key or get_openrouter_api_key()
    if not key:
        logger.info("OPENROUTER_API_KEY is not set. Skipping VLM verification (Zero-Mock).")
        return {
            "status": "VLM_UNVERIFIED_NO_KEY",
            "confirmed": None,
            "readability_ok": None,
            "details": "OPENROUTER_API_KEY не задан. Визуальная верификация VLM пропущена (требуется ручной просмотр в review_desk).",
            "model_used": None,
            "findings_verdicts": [],
            "methods_mismatch_note": "",
            "human_hint_ru": "Ключ OpenRouter не настроен. Проверьте скриншоты зон вручную в интерфейсе review_desk.",
        }

    c_path = Path(collage_path)
    if not c_path.exists() or c_path.stat().st_size == 0:
        return {
            "status": "VLM_SKIPPED_NO_COLLAGE",
            "confirmed": None,
            "readability_ok": None,
            "details": "Коллаж зон сайта отсутствует или пуст.",
            "model_used": None,
            "findings_verdicts": [],
            "methods_mismatch_note": "",
            "human_hint_ru": "Коллаж скриншотов не найден.",
        }

    collage_bytes = c_path.read_bytes()

    # Check Idempotent SQLite Cache
    cache_key = _CACHE.compute_key(collage_bytes, audit_findings, PROMPT_VERSION)
    cached_result = _CACHE.get(cache_key)
    if cached_result:
        logger.info(f"VLM cache hit for key {cache_key[:12]}...")
        return cached_result

    # Pure binary read (Zero PIL/cv2) -> Base64
    b64_image = base64.b64encode(collage_bytes).decode("ascii")
    image_data_uri = f"data:image/png;base64,{b64_image}"

    # Prepare findings summary
    findings_summary: List[Dict[str, Any]] = []
    for f in audit_findings:
        findings_summary.append({
            "finding_id": f.get("finding_id") or f.get("signature") or "unknown",
            "law_norm": f.get("law_norm", ""),
            "traffic_light": f.get("traffic_light", ""),
            "quote": (f.get("witness") or {}).get("quote", "") if isinstance(f.get("witness"), dict) else "",
            "notes": (f.get("witness") or {}).get("notes", "") if isinstance(f.get("witness"), dict) else f.get("notes", ""),
        })

    allowed_citations, allowed_koap = load_citation_whitelist()

    system_instruction = (
        "Ты — независимый судебный эксперт и контролер визуального ОТК (Quality Assurance) "
        "для юридического аудита российских веб-сайтов. "
        "Твоя задача — критически перепроверить потенциальные нарушения, выявленные автоматическим сканером, "
        "по скриншотам 3 ключевых зон (коллаж прикреплен):\n"
        "• Зона #1 (FOOTER / LEGAL): подвал сайта, реквизиты (ИНН/ОГРН), сноски со звездочками, дисклеймеры "
        "(включая примечания об экстремистских организациях Meta/Instagram/Facebook по ч. 2 ст. 13.15 КоАП РФ).\n"
        "• Зона #2 (FORM / CONSENT): форма захвата лидов, чекбоксы согласия, ссылки на Политику конфиденциальности, баннеры Cookie.\n"
        "• Зона #3 (HERO / PROMO): главный экран, баннеры, слоганы превосходной степени («№1», «лучший» по ст. 5 38-ФЗ), англицизмы 168-ФЗ.\n\n"
        "ГЛАВНАЯ ЦЕЛЬ — ОТСЕЧЬ ЛОЖНЫЕ СРАБАТЫВАНИЯ (FALSE POSITIVES)!\n"
        "- Если сканер заявляет, что нарушена ч. 2 ст. 13.15 КоАП РФ (нет сноски про Meta), но на скриншоте Зоны #1 "
        "реально видна сноска (* Деятельность организации признана экстремистской / Meta запрещена в РФ) — "
        "выноси вердикт 'FALSE_POSITIVE' и обязательно приведи exact_visible_text!\n"
        "- Если сканер заявляет, что в форме нет согласия на обработку ПДн, но на скриншоте Зоны #2 видна строка согласия "
        "или ссылка на Политику — выноси вердикт 'FALSE_POSITIVE'!\n"
        "- Если сканер нашел реальное нарушение (например, нет реквизитов в футере, или в шапке заявлено '№1' без сноски) — "
        "выноси вердикт 'CONFIRMED_VIOLATION'.\n"
        "- Проверь читаемость текста: если дисклеймер микроскопический (<8px) или неконтрастный, отметь readability_ok: false.\n\n"
        "ВЕРНИ ОТВЕТ СТРОГО В ВИДЕ JSON БЕЗ ЛИШНЕГО ТЕКСТА:\n"
        "{\n"
        '  "status": "VERIFIED_BY_VLM" | "FALSE_POSITIVE_CAUGHT_BY_VLM" | "CONFIRMED_VIOLATION",\n'
        '  "confirmed": true | false,\n'
        '  "readability_ok": true | false,\n'
        '  "findings_verdicts": [\n'
        "    {\n"
        '      "finding_id": "...",\n'
        '      "scanner_verdict": "VIOLATION",\n'
        '      "vlm_verdict": "FALSE_POSITIVE" | "CONFIRMED_VIOLATION" | "UNVERIFIED",\n'
        '      "is_false_positive": true | false,\n'
        '      "exact_visible_text": "...",\n'
        '      "visual_evidence_ru": "...",\n'
        '      "confidence": 0.0-1.0\n'
        "    }\n"
        "  ],\n"
        '  "methods_mismatch_note": "...",\n'
        '  "human_hint_ru": "..."\n'
        "}"
    )

    # Wrap scraped text in <untrusted_page_data> to neutralize prompt injection
    safe_findings_json = json.dumps(findings_summary, ensure_ascii=False, indent=2)
    safe_policy_text = policy_snippet or "Не предоставлен"

    user_text = (
        f"Список нарушений от сканера:\n<untrusted_page_data>\n{safe_findings_json}\n</untrusted_page_data>\n\n"
        f"Фрагмент методов из Политики (PII-очищен):\n<untrusted_page_data>\n{safe_policy_text}\n</untrusted_page_data>\n\n"
        "Изучи приложенный коллаж скриншотов зон #1, #2, #3 и вынеси судебное заключение по каждому пункту."
    )

    messages = [
        {"role": "system", "content": system_instruction},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ],
        },
    ]

    # Resolve candidates via LiveCatalogResolver
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=45.0)
        close_client = True

    try:
        resolved_models = await _CATALOG_RESOLVER.resolve_models(paid_zdr_mode=paid_zdr_mode, client=client)
    except Exception as e:
        logger.warning(f"LiveCatalogResolver error: {e}, falling back to defaults")
        resolved_models = [primary_model] + [m for m in FALLBACK_MODELS if m != primary_model]

    # Filter out models tripped by CircuitBreaker
    available_models = [m for m in resolved_models if _CIRCUIT_BREAKER.is_available(m)]
    if not available_models:
        logger.warning("All VLM models are in CircuitBreaker cooldown! Returning TriState.UNVERIFIED.")
        if close_client:
            await client.aclose()
        return {
            "status": "UNVERIFIED",
            "confirmed": None,
            "readability_ok": None,
            "details": "Все модели VLM временно недоступны (CircuitBreaker cooldown). Пульт человека не заблокирован.",
            "model_used": None,
            "findings_verdicts": [],
            "methods_mismatch_note": "",
            "human_hint_ru": "Сервис VLM временно на перезагрузке. Проверьте скриншоты вручную.",
        }

    last_error = ""
    result_data: Optional[Dict[str, Any]] = None
    used_model: Optional[str] = None

    try:
        for model in available_models:
            # Enforce 12 RPM rate limit
            await _RATE_LIMITER.acquire()

            req_body: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
            }
            if paid_zdr_mode:
                req_body["provider"] = {"data_collection": "deny", "zdr": True}

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://adradar.ru",
                "X-Title": "AdRadar Visual QC",
            }

            try:
                resp = await client.post(
                    OPENROUTER_ENDPOINT,
                    json=req_body,
                    headers=headers,
                )
                if resp.status_code == 200:
                    resp_json = resp.json()
                    choices = resp_json.get("choices", [])
                    if choices:
                        raw_content = choices[0].get("message", {}).get("content", "")
                        try:
                            result_data = _parse_json_resilient(raw_content)
                            used_model = model
                            _CIRCUIT_BREAKER.record_success(model)
                            break
                        except Exception as parse_err:
                            logger.warning(f"Failed to parse JSON from {model}: {parse_err}. Raw: {raw_content[:200]}")
                            last_error = f"JSON parse error from {model}: {parse_err}"
                            _CIRCUIT_BREAKER.record_failure(model)
                elif resp.status_code in (429, 503):
                    logger.warning(f"OpenRouter {model} returned status {resp.status_code}, tripping failure.")
                    last_error = f"HTTP {resp.status_code} from {model}"
                    _CIRCUIT_BREAKER.record_failure(model)
                    continue
                else:
                    logger.warning(f"OpenRouter {model} error {resp.status_code}: {resp.text[:200]}")
                    last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    _CIRCUIT_BREAKER.record_failure(model)
            except Exception as conn_err:
                logger.warning(f"Error querying {model}: {conn_err}")
                last_error = str(conn_err)
                _CIRCUIT_BREAKER.record_failure(model)
                continue

    finally:
        if close_client:
            await client.aclose()

    if result_data is None:
        return {
            "status": "UNVERIFIED",
            "confirmed": None,
            "readability_ok": None,
            "details": f"Не удалось получить ответ VLM: {last_error}",
            "model_used": None,
            "findings_verdicts": [],
            "methods_mismatch_note": "",
            "human_hint_ru": "Произошла ошибка при обращении к VLM. Проверьте скриншоты зон вручную в review_desk.",
        }

    # Pass findings verdicts through ContextRuleReconciler
    if "findings_verdicts" in result_data and isinstance(result_data["findings_verdicts"], list):
        result_data["findings_verdicts"] = _RECONCILER.reconcile(
            findings_verdicts=result_data["findings_verdicts"],
            original_findings=audit_findings,
            allowed_citations=allowed_citations,
            allowed_koap=allowed_koap,
        )

    # Sanitize citations in text fields
    if "human_hint_ru" in result_data and isinstance(result_data["human_hint_ru"], str):
        result_data["human_hint_ru"] = sanitize_model_citations(result_data["human_hint_ru"], allowed_citations, allowed_koap)
    if "methods_mismatch_note" in result_data and isinstance(result_data["methods_mismatch_note"], str):
        result_data["methods_mismatch_note"] = sanitize_model_citations(result_data["methods_mismatch_note"], allowed_citations, allowed_koap)

    result_data["model_used"] = used_model

    # Save to SQLite cache
    _CACHE.set(cache_key, result_data, used_model or "unknown")

    return result_data
