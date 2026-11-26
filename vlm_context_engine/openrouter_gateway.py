"""
VLM Context Engine: Dual-Track OpenRouter Multimodal Gateway.
Track A: Decisions API (POST /api/alpha/decisions or fallback on structured decider).
Track B: VLM Chat Completions API (POST /api/v1/chat/completions).
Zero Data Retention (ZDR) header on paid tiers.
Token-Bucket RateLimiter (12 RPM), CircuitBreaker (3 failures -> 10m cooldown),
SQLite Idempotent Cache, Citation Whitelist Sanitizer, and TriState.UNVERIFIED Fallback.
Strict Zero-Vision-Library isolation (NO cv2, NO PIL).
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import httpx
import yaml

from legal_sheaf import config as legal_config
from vlm_context_engine.live_catalog import (
    LiveCatalogResolver,
    FinancialLockViolation,
    DEFAULT_PRIMARY_FREE_MODEL,
    DEFAULT_FALLBACK_FREE_MODELS,
)

logger = logging.getLogger("vlm_context_engine.gateway")

ROOT_DIR = Path(__file__).resolve().parent.parent


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    return Path(legal_config.ADRADAR_DATA_DIR).resolve()

OPENROUTER_CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
PROMPT_VERSION = "2026.11-v3-dualtrack"


# =============================================================================
# 1. API KEY RESOLUTION & CITATION WHITELIST
# =============================================================================
def _read_env_key(env_path: Path, target_key: str) -> str:
    """Safely extracts target_key from a .env file without mutating global os.environ."""
    if not env_path.exists():
        return ""
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == target_key:
                val = v.strip().strip('"').strip("'")
                if val:
                    return val
    except Exception:
        pass
    return ""


def _parse_api_key_list(raw: str) -> List[str]:
    """Splits comma/semicolon/whitespace separated OpenRouter keys and deduplicates preserving order."""
    keys: List[str] = []
    for part in re.split(r"[\s,;]+", str(raw or "").strip()):
        clean = part.strip().strip('"').strip("'")
        if clean and clean not in keys:
            keys.append(clean)
    return keys


def _mask_api_key(key: str) -> str:
    """Returns a non-sensitive masked identifier for logging (never logs full secret)."""
    if len(key) > 18:
        return f"{key[:12]}...{key[-4:]}"
    return "***" if key else ""


def get_openrouter_api_keys() -> List[str]:
    """
    Retrieves OpenRouter API key pool from environment or ADRADAR_DATA_DIR/.env / repo .env / CWD/.env.
    Supports both single OPENROUTER_API_KEY and comma/newline-separated OPENROUTER_API_KEYS.
    """
    env_keys = _parse_api_key_list(os.environ.get("OPENROUTER_API_KEY", ""))
    env_pool = _parse_api_key_list(os.environ.get("OPENROUTER_API_KEYS", ""))
    if env_keys or env_pool:
        merged: List[str] = []
        for k in env_keys + env_pool:
            if k not in merged:
                merged.append(k)
        return merged

    candidates = [
        _active_data_dir() / ".env",
        ROOT_DIR / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        single_val = _read_env_key(env_path, "OPENROUTER_API_KEY")
        multi_val = _read_env_key(env_path, "OPENROUTER_API_KEYS")
        merged = []
        for k in _parse_api_key_list(single_val) + _parse_api_key_list(multi_val):
            if k not in merged:
                merged.append(k)
        if merged:
            return merged

    return []


def get_openrouter_api_key() -> str:
    """Retrieves primary OPENROUTER_API_KEY from environment, ADRADAR_DATA_DIR/.env, repo .env, or CWD/.env."""
    keys = get_openrouter_api_keys()
    return keys[0] if keys else ""


def load_citation_whitelist() -> Tuple[set, set]:
    """Loads closed whitelist of verified court cases, agency letters, and allowed KoAP articles."""
    candidates = [
        Path.cwd() / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml",
        ROOT_DIR / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml",
        Path.cwd() / "config" / "verified_citations_whitelist.yaml",
        ROOT_DIR / "config" / "verified_citations_whitelist.yaml",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                cases = set(data.get("verified_court_cases", []))
                letters = set(data.get("verified_agency_letters", []))
                koap_direct = data.get("verified_koap_articles", []) or []
                koap_nested = (data.get("verified_koap_and_laws") or {}).get("koap_articles", []) or []
                allowed_koap = set(str(x) for x in (list(koap_direct) + list(koap_nested)))
                citations = cases | letters
                if citations or allowed_koap:
                    return citations, allowed_koap
            except Exception as e:
                logger.warning(f"Failed to read citation whitelist {p}: {e}")
    return set(), set()


def _sanitize_text_citations(text: str, allowed_citations: set, allowed_koap: Optional[set] = None) -> str:
    """Sanitizes a single string against verified citations and KoAP articles."""
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


def sanitize_model_citations(
    text_or_payload: Union[str, Dict[str, Any]],
    allowed_citations: set,
    allowed_koap: Optional[set] = None,
) -> Union[str, Dict[str, Any]]:
    """
    Scans model output (string or VLM response dict) for unverified court case numbers,
    agency letters, and unauthorized KoAP articles.
    When passed a dict, sanitizes human_hint_ru, methods_mismatch_note, and each item
    inside findings_verdicts[*]['visual_evidence_ru'] (plus exact_visible_text/reconciliation_note).
    """
    if isinstance(text_or_payload, dict):
        cleaned_payload = dict(text_or_payload)
        for top_key in ("human_hint_ru", "methods_mismatch_note", "details"):
            val = cleaned_payload.get(top_key)
            if isinstance(val, str):
                cleaned_payload[top_key] = _sanitize_text_citations(val, allowed_citations, allowed_koap)

        verdicts = cleaned_payload.get("findings_verdicts")
        if isinstance(verdicts, list):
            new_verdicts = []
            for fv in verdicts:
                if isinstance(fv, dict):
                    fv_copy = dict(fv)
                    for sub_key in ("visual_evidence_ru", "exact_visible_text", "reconciliation_note"):
                        sub_val = fv_copy.get(sub_key)
                        if isinstance(sub_val, str):
                            fv_copy[sub_key] = _sanitize_text_citations(sub_val, allowed_citations, allowed_koap)
                    new_verdicts.append(fv_copy)
                else:
                    new_verdicts.append(fv)
            cleaned_payload["findings_verdicts"] = new_verdicts
        return cleaned_payload

    return _sanitize_text_citations(text_or_payload, allowed_citations, allowed_koap)


def extract_json_resilient(raw_response: str) -> Dict[str, Any]:
    """
    Extracts JSON dictionary from raw model response.
    - Strips <think>...</think> reasoning traces.
    - Strips markdown code blocks.
    - Checks for top-level or nested error payloads.
    - Parses outermost { ... } structure.
    """
    if not raw_response or not isinstance(raw_response, str):
        raise ValueError("Empty or invalid raw response provided.")

    cleaned = raw_response.strip()

    # Strip <think>...</think> reasoning tags
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", cleaned).strip()

    # Strip markdown fences
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if m:
            cleaned = m.group(1).strip()

    # Attempt direct json.loads
    parsed: Optional[Dict[str, Any]] = None
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            parsed = data
    except Exception:
        pass

    if parsed is None:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(cleaned[start : end + 1])
        else:
            raise ValueError(f"No valid JSON object found in response: {cleaned[:150]}")

    if not isinstance(parsed, dict):
        raise ValueError(f"Parsed JSON is not a dictionary: {type(parsed).__name__}")

    # Check for API error structure
    if "error" in parsed:
        err_msg = parsed["error"]
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(err_msg))
        raise ValueError(f"API Error payload received: {err_msg}")

    return parsed


def _parse_json_resilient(text: str) -> Dict[str, Any]:
    """Backward compatibility alias for extract_json_resilient."""
    return extract_json_resilient(text)


# =============================================================================
# 2. CIRCUIT BREAKER & RATE LIMITER
# =============================================================================
class CircuitBreaker:
    """
    Tracks consecutive errors (429, 503, timeout) per model with SQLite persistence.
    Trips into cooldown for 10 minutes after threshold failures.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 600.0,
        db_path: Optional[Path] = None,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.db_path = db_path or (_active_data_dir() / "db" / "vlm_cache.sqlite")
        self._failures: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                cur = conn.execute("SELECT model_id, consecutive_failures, cooldown_until FROM breaker_state")
                for row in cur.fetchall():
                    model, fails, cd = row[0], row[1], row[2]
                    self._failures[model] = fails
                    self._cooldown_until[model] = cd
        except Exception as e:
            logger.warning(f"Failed to load breaker_state from DB: {e}")

    def is_available(self, model: str) -> bool:
        cd = self._cooldown_until.get(model, 0.0)
        return time.time() >= cd

    def record_failure(self, model: str) -> None:
        self._failures[model] = self._failures.get(model, 0) + 1
        now = time.time()
        cd = 0.0
        if self._failures[model] >= self.failure_threshold:
            cd = now + self.cooldown_seconds
            self._cooldown_until[model] = cd
            logger.warning(
                f"Circuit breaker TRIPPED for model {model} (failed {self._failures[model]} times). "
                f"In cooldown for {self.cooldown_seconds}s."
            )
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS breaker_state (
                        model_id TEXT PRIMARY KEY,
                        consecutive_failures INTEGER DEFAULT 0,
                        cooldown_until REAL DEFAULT 0.0,
                        updated_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO breaker_state (model_id, consecutive_failures, cooldown_until, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        consecutive_failures = excluded.consecutive_failures,
                        cooldown_until = excluded.cooldown_until,
                        updated_at = excluded.updated_at
                """, (model, self._failures[model], cd, now))
        except Exception as e:
            logger.warning(f"Failed to persist breaker failure for {model}: {e}")

    def record_success(self, model: str) -> None:
        self._failures[model] = 0
        self._cooldown_until.pop(model, None)
        now = time.time()
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS breaker_state (
                        model_id TEXT PRIMARY KEY,
                        consecutive_failures INTEGER DEFAULT 0,
                        cooldown_until REAL DEFAULT 0.0,
                        updated_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO breaker_state (model_id, consecutive_failures, cooldown_until, updated_at)
                    VALUES (?, 0, 0.0, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        consecutive_failures = 0,
                        cooldown_until = 0.0,
                        updated_at = excluded.updated_at
                """, (model, now))
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_requests (
                        day_date TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        request_count INTEGER DEFAULT 0,
                        PRIMARY KEY (day_date, model_id)
                    )
                """)
                conn.execute("""
                    INSERT INTO daily_requests (day_date, model_id, request_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(day_date, model_id) DO UPDATE SET
                        request_count = request_count + 1
                """, (today_str, model))
        except Exception as e:
            logger.warning(f"Failed to persist breaker success for {model}: {e}")


class TokenBucketRateLimiter:
    """Token-Bucket rate limiter enforcing a strict 12 RPM maximum."""

    def __init__(self, max_rpm: int = 12):
        self.max_rpm = max_rpm
        self.window_seconds = 60.0
        self._timestamps: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < self.window_seconds]
            if len(self._timestamps) >= self.max_rpm:
                oldest = self._timestamps[0]
                sleep_time = self.window_seconds - (now - oldest) + 0.1
                if sleep_time > 0:
                    logger.info(f"Rate limit ({self.max_rpm} RPM) reached. Waiting {sleep_time:.2f}s...")
                    await asyncio.sleep(sleep_time)
            self._timestamps.append(time.time())


# =============================================================================
# 3. SQLITE IDEMPOTENT CACHE
# =============================================================================
class VlmSqliteCache:
    """Idempotent cache for VLM responses keyed by SHA-256(url + collage_bytes + prompt_ver)."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (_active_data_dir() / "db" / "vlm_cache.sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vlm_cache (
                        cache_key TEXT PRIMARY KEY,
                        response_json TEXT NOT NULL,
                        model_used TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS breaker_state (
                        model_id TEXT PRIMARY KEY,
                        consecutive_failures INTEGER DEFAULT 0,
                        cooldown_until REAL DEFAULT 0.0,
                        updated_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_requests (
                        day_date TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        request_count INTEGER DEFAULT 0,
                        PRIMARY KEY (day_date, model_id)
                    )
                """)
        except Exception as e:
            logger.warning(f"Failed to initialize VLM cache DB: {e}")

    @staticmethod
    def compute_key(payload_bytes: bytes, findings: Any, prompt_ver: str = PROMPT_VERSION) -> str:
        h = hashlib.sha256()
        h.update(payload_bytes)
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
# 4. OPENROUTER GATEWAY (Track A & Track B)
# =============================================================================
class OpenRouterGateway:
    """
    Dual-track OpenRouter gateway for multimodal and decision verification.
    Track A: Decisions API (POST /api/alpha/decisions or schema-guided decider).
    Track B: VLM Chat API (POST /api/v1/chat/completions).
    Zero Data Retention (ZDR) on paid tier.
    """

    def __init__(
        self,
        api_key: Optional[Union[str, List[str]]] = None,
        catalog_resolver: Optional[LiveCatalogResolver] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        cache: Optional[VlmSqliteCache] = None,
        api_keys: Optional[List[str]] = None,
    ):
        if api_keys is not None:
            resolved_keys = _parse_api_key_list(",".join(api_keys))
        elif api_key is not None:
            if isinstance(api_key, (list, tuple)):
                resolved_keys = _parse_api_key_list(",".join(str(x) for x in api_key))
            elif str(api_key).strip() == "":
                resolved_keys = []
            else:
                resolved_keys = _parse_api_key_list(str(api_key))
        else:
            resolved_keys = get_openrouter_api_keys()

        self.api_keys: List[str] = resolved_keys
        self.api_key: str = resolved_keys[0] if resolved_keys else ""
        self._key_cursor: int = 0
        self._exhausted_keys: Set[str] = set()
        self.catalog_resolver = catalog_resolver or LiveCatalogResolver()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(max_rpm=12)
        self.cache = cache or VlmSqliteCache()
        self.allowed_citations, self.allowed_koap = load_citation_whitelist()

    def _active_key_pool(self) -> List[str]:
        """Returns active API keys rotated from current cursor, excluding exhausted keys."""
        if not self.api_keys:
            return []
        n = len(self.api_keys)
        start = self._key_cursor % n
        ordered = [self.api_keys[(start + i) % n] for i in range(n)]
        return [k for k in ordered if k not in self._exhausted_keys]

    def _rotate_past_key(self, used_key: str) -> None:
        if used_key in self.api_keys:
            idx = self.api_keys.index(used_key)
            self._key_cursor = (idx + 1) % len(self.api_keys)
            if self._active_key_pool():
                self.api_key = self._active_key_pool()[0]

    @staticmethod
    def _build_headers(api_key: str, title: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://adradar.ru",
            "X-Title": title,
        }

    # -------------------------------------------------------------------------
    # Track A: Decisions API
    # -------------------------------------------------------------------------
    async def track_a_decide(
        self,
        instructions: str,
        context_data: Dict[str, Any],
        decision_questions: List[Dict[str, Any]],
        image_bytes: Optional[bytes] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        """
        Executes structured decision arbitration.
        Attempts POST /api/alpha/decisions; falls back to structured chat completions if alpha is unavailable.
        """
        active_keys = self._active_key_pool()
        if not active_keys:
            return {
                "status": "UNVERIFIED",
                "decisions": [],
                "error": "OPENROUTER_API_KEY is not set (Zero-Mock active).",
            }

        # Check cache
        cache_bytes = image_bytes or b""
        cache_key = self.cache.compute_key(cache_bytes, decision_questions, f"{PROMPT_VERSION}_track_a")
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        close_client = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=35.0)
            close_client = True

        # Attempt Track A Alpha Decisions endpoint
        decisions_payload = {
            "instructions": instructions,
            "context": context_data,
            "questions": decision_questions,
        }

        for active_key in active_keys:
            try:
                await self.rate_limiter.acquire()
                resp = await c.post(
                    OPENROUTER_DECISIONS_ENDPOINT,
                    json=decisions_payload,
                    headers=self._build_headers(active_key, "AdRadar Decisions Gate"),
                )
                if resp.status_code == 200:
                    result = resp.json()
                    self._rotate_past_key(active_key)
                    self.cache.set(cache_key, result, "openrouter/alpha/decisions")
                    return result
                elif resp.status_code in (401, 402, 403):
                    logger.warning(f"OpenRouter key {_mask_api_key(active_key)} rejected on Track A (HTTP {resp.status_code}); rotating.")
                    self._exhausted_keys.add(active_key)
                    self._rotate_past_key(active_key)
                    continue
                elif resp.status_code == 429:
                    logger.warning(f"OpenRouter key {_mask_api_key(active_key)} rate-limited on Track A (HTTP 429); rotating.")
                    self._rotate_past_key(active_key)
                    continue
                break
            except Exception as e:
                logger.info(f"Track A alpha endpoint not available or failed ({e}), falling back to structured Chat VLM.")
                break

        # Fallback to Track B with Decisions JSON Schema
        fallback_prompt = (
            f"Инструкция арбитража:\n{instructions}\n\n"
            f"Контекст:\n<untrusted_page_data>\n{json.dumps(context_data, ensure_ascii=False, indent=2)}\n</untrusted_page_data>\n\n"
            f"Вопросы для решения:\n<untrusted_page_data>\n{json.dumps(decision_questions, ensure_ascii=False, indent=2)}\n</untrusted_page_data>\n\n"
            "Вынеси обоснованное решение по каждому вопросу строго в формате JSON:\n"
            "{\n"
            '  "status": "DECIDED",\n'
            '  "decisions": [\n'
            '    {"question_id": "...", "answer": "YES|NO|UNCLEAR", "justification_ru": "...", "confidence": 0.0-1.0}\n'
            "  ]\n"
            "}"
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": "Ты — беспристрастный арбитр правовых норм и фактических доказательств."},
        ]

        if image_bytes:
            b64_img = base64.b64encode(image_bytes).decode("ascii")
            data_uri = f"data:image/png;base64,{b64_img}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": fallback_prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            })
        else:
            messages.append({"role": "user", "content": fallback_prompt})

        try:
            models = await self.catalog_resolver.resolve_models(paid_zdr_mode=False, client=c)
            available = [m for m in models if self.circuit_breaker.is_available(m)]
            if not available:
                return {"status": "UNVERIFIED", "decisions": [], "error": "All models in cooldown."}

            for model in available:
                keys_for_model = self._active_key_pool()
                if not keys_for_model:
                    break
                all_rate_limited = True
                for active_key in keys_for_model:
                    await self.rate_limiter.acquire()
                    try:
                        resp = await c.post(
                            OPENROUTER_CHAT_ENDPOINT,
                            json={"model": model, "messages": messages, "temperature": 0.1},
                            headers=self._build_headers(active_key, "AdRadar Decisions Gate"),
                        )
                        if resp.status_code == 200:
                            raw = resp.json()["choices"][0]["message"]["content"]
                            parsed = _parse_json_resilient(raw)
                            self._rotate_past_key(active_key)
                            self.circuit_breaker.record_success(model)
                            self.cache.set(cache_key, parsed, model)
                            return parsed
                        elif resp.status_code in (401, 402, 403):
                            logger.warning(f"OpenRouter key {_mask_api_key(active_key)} unauthorized/empty ({resp.status_code}); rotating.")
                            self._exhausted_keys.add(active_key)
                            self._rotate_past_key(active_key)
                            continue
                        elif resp.status_code == 429:
                            logger.warning(f"OpenRouter key {_mask_api_key(active_key)} rate-limited on {model} (429); rotating.")
                            self._rotate_past_key(active_key)
                            continue
                        else:
                            all_rate_limited = False
                            if resp.status_code == 503:
                                self.circuit_breaker.record_failure(model)
                            break
                    except Exception as e:
                        all_rate_limited = False
                        self.circuit_breaker.record_failure(model)
                        logger.warning(f"Decisions fallback error on {model}: {e}")
                        break
                if all_rate_limited:
                    self.circuit_breaker.record_failure(model)
        finally:
            if close_client:
                await c.aclose()

        return {"status": "UNVERIFIED", "decisions": [], "error": "Decisions arbitration failed."}

    # -------------------------------------------------------------------------
    # Track B: VLM Chat Completions API
    # -------------------------------------------------------------------------
    async def track_b_chat_verify(
        self,
        collage_path: Union[str, Path],
        audit_findings: List[Dict[str, Any]],
        policy_snippet: Optional[str] = None,
        paid_mode: bool = False,
        client: Optional[httpx.AsyncClient] = None,
        primary_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes multimodal visual verification of 3 trigger zones in collage.
        Enforces ZDR header in paid_mode.
        Applies prompt injection encapsulation, 12 RPM rate limiter, CircuitBreaker, and Citation Sanitizer.
        """
        if not self._active_key_pool():
            logger.info("OPENROUTER_API_KEY is not set. Skipping VLM verification (Zero-Mock active).")
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
        if not c_path.exists():
            if c_path.suffix == ".png" and c_path.with_suffix(".jpg").exists():
                c_path = c_path.with_suffix(".jpg")
            elif c_path.suffix == ".jpg" and c_path.with_suffix(".png").exists():
                c_path = c_path.with_suffix(".png")

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

        # Check Cache
        cache_key = self.cache.compute_key(collage_bytes, audit_findings, PROMPT_VERSION)
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"VLM cache hit for key {cache_key[:12]}...")
            return cached

        b64_image = base64.b64encode(collage_bytes).decode("ascii")
        mime_type = "image/jpeg" if (str(c_path).lower().endswith((".jpg", ".jpeg")) or collage_bytes.startswith(b"\xff\xd8\xff")) else "image/png"
        image_data_uri = f"data:{mime_type};base64,{b64_image}"

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
            "- Проверь читаемость текста: если дисклеймер микроскопический (<8px) или неконтрастный, отметь readability_grade: 'MICRO_FONT_UNREADABLE' "
            "или readability_ok: false.\n\n"
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
            '      "element_visually_present": true | false,\n'
            '      "readability_grade": "CLEARLY_READABLE" | "MICRO_FONT_UNREADABLE" | "OVERLAPPED_BY_UI" | "NOT_FOUND",\n'
            '      "exact_visible_text": "...",\n'
            '      "visual_evidence_ru": "...",\n'
            '      "confidence": 0.0-1.0\n'
            "    }\n"
            "  ],\n"
            '  "page_genre": "COMMERCIAL_STORE" | "PORTFOLIO_CASE" | "HR_VACANCY" | "B2B_CORPORATE_ONLY" | "INFORMATIONAL",\n'
            '  "methods_mismatch_note": "...",\n'
            '  "human_hint_ru": "..."\n'
            "}"
        )

        # Encapsulate untrusted data
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

        close_client = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=45.0)
            close_client = True

        try:
            resolved_models = await self.catalog_resolver.resolve_models(paid_zdr_mode=paid_mode, client=c)
        except Exception as e:
            logger.warning(f"Catalog resolution error: {e}")
            resolved_models = [DEFAULT_PRIMARY_FREE_MODEL] + DEFAULT_FALLBACK_FREE_MODELS

        if primary_model and primary_model in resolved_models:
            resolved_models = [primary_model] + [m for m in resolved_models if m != primary_model]

        available = [m for m in resolved_models if self.circuit_breaker.is_available(m)]
        if not available:
            logger.warning("All VLM models in CircuitBreaker cooldown! Returning TriState.UNVERIFIED.")
            if close_client:
                await c.aclose()
            return {
                "status": "UNVERIFIED",
                "confirmed": None,
                "readability_ok": None,
                "details": "Все модели VLM временно недоступны (CircuitBreaker cooldown).",
                "model_used": None,
                "findings_verdicts": [],
                "methods_mismatch_note": "",
                "human_hint_ru": "Сервис VLM временно на перезагрузке. Проверьте скриншоты вручную.",
            }

        result_data: Optional[Dict[str, Any]] = None
        used_model: Optional[str] = None
        last_error = ""

        try:
            for model in available:
                keys_for_model = self._active_key_pool()
                if not keys_for_model:
                    last_error = "Все ключи OpenRouter исчерпаны или отклонены (401/402/403)."
                    break

                req_body: Dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                }
                if paid_mode:
                    # Mandatory Zero Data Retention (ZDR) configuration
                    req_body["provider"] = {"data_collection": "deny", "zdr": True}

                model_rate_limited_all_keys = True
                for active_key in keys_for_model:
                    await self.rate_limiter.acquire()
                    headers = self._build_headers(active_key, "AdRadar Visual QC")

                    try:
                        resp = await c.post(
                            OPENROUTER_CHAT_ENDPOINT,
                            json=req_body,
                            headers=headers,
                        )
                        if resp.status_code == 200:
                            raw_content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                            try:
                                result_data = _parse_json_resilient(raw_content)
                                used_model = model
                                model_rate_limited_all_keys = False
                                self._rotate_past_key(active_key)
                                self.circuit_breaker.record_success(model)
                                break
                            except Exception as parse_err:
                                logger.warning(f"Failed to parse JSON from {model}: {parse_err}")
                                last_error = f"JSON parse error from {model}: {parse_err}"
                                model_rate_limited_all_keys = False
                                self.circuit_breaker.record_failure(model)
                                break
                        elif resp.status_code in (401, 402, 403):
                            logger.warning(
                                f"OpenRouter key {_mask_api_key(active_key)} rejected on {model} "
                                f"(HTTP {resp.status_code}); rotating key."
                            )
                            last_error = f"HTTP {resp.status_code} on key {_mask_api_key(active_key)}"
                            self._exhausted_keys.add(active_key)
                            self._rotate_past_key(active_key)
                            continue
                        elif resp.status_code == 429:
                            logger.warning(
                                f"OpenRouter {model} returned HTTP 429 on key {_mask_api_key(active_key)}; rotating key."
                            )
                            last_error = f"HTTP 429 from {model} on key {_mask_api_key(active_key)}"
                            self._rotate_past_key(active_key)
                            continue
                        elif resp.status_code == 503:
                            logger.warning(f"OpenRouter {model} returned HTTP 503")
                            last_error = f"HTTP 503 from {model}"
                            model_rate_limited_all_keys = False
                            self.circuit_breaker.record_failure(model)
                            break
                        else:
                            logger.warning(f"OpenRouter {model} error {resp.status_code}: {resp.text[:200]}")
                            last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                            model_rate_limited_all_keys = False
                            self.circuit_breaker.record_failure(model)
                            break
                    except Exception as conn_err:
                        logger.warning(f"Connection error to {model}: {conn_err}")
                        last_error = str(conn_err)
                        model_rate_limited_all_keys = False
                        self.circuit_breaker.record_failure(model)
                        break

                if result_data is not None:
                    break
                if model_rate_limited_all_keys:
                    self.circuit_breaker.record_failure(model)
        finally:
            if close_client:
                await c.aclose()

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

        # Sanitize citations in top-level text fields and nested findings_verdicts[*].visual_evidence_ru
        sanitized_result = sanitize_model_citations(
            result_data, self.allowed_citations, self.allowed_koap
        )
        if isinstance(sanitized_result, dict):
            result_data = sanitized_result

        result_data["model_used"] = used_model

        # Save to SQLite cache
        self.cache.set(cache_key, result_data, used_model or "unknown")
        return result_data


# Lazy singleton instance (never writes to disk on import)
_DEFAULT_GATEWAY: Optional[OpenRouterGateway] = None
_DEFAULT_GATEWAY_DIR: Optional[Path] = None


def get_default_gateway() -> OpenRouterGateway:
    """Returns a lazy singleton OpenRouterGateway bound to the active ADRADAR_DATA_DIR."""
    global _DEFAULT_GATEWAY, _DEFAULT_GATEWAY_DIR
    cur_dir = _active_data_dir()
    if _DEFAULT_GATEWAY is None or _DEFAULT_GATEWAY_DIR != cur_dir or not _DEFAULT_GATEWAY._active_key_pool():
        _DEFAULT_GATEWAY = OpenRouterGateway()
        _DEFAULT_GATEWAY_DIR = cur_dir
    return _DEFAULT_GATEWAY


async def verify_visual_triggers(
    collage_path: Union[str, Path],
    audit_findings: List[Dict[str, Any]],
    policy_snippet: Optional[str] = None,
    paid_zdr_mode: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    api_key: Optional[str] = None,
    primary_model: Optional[str] = None,
    provider: str = "auto",
) -> Dict[str, Any]:
    """
    Unified entry point for visual verification callers.
    Defaults to Direct Google Gemini API (`provider="auto"` when `GEMINI_API_KEY` is configured),
    falling back to OpenRouter when requested or when only OpenRouter keys exist.
    """
    prov = str(provider or "auto").strip().lower()
    use_gemini = False
    if prov == "gemini":
        use_gemini = True
    elif prov == "openrouter":
        use_gemini = False
    else:
        # auto routing: explicit non-AIza key preserves OpenRouter callers/tests
        if api_key is not None:
            use_gemini = str(api_key).strip().startswith("AIza")
        else:
            from vlm_context_engine.gemini_gateway import get_gemini_api_keys
            use_gemini = bool(get_gemini_api_keys())

    if use_gemini:
        from vlm_context_engine.gemini_gateway import GeminiGateway, get_default_gemini_gateway
        gateway = GeminiGateway(api_key=api_key) if api_key is not None else get_default_gemini_gateway()
        return await gateway.track_b_chat_verify(
            collage_path=collage_path,
            audit_findings=audit_findings,
            policy_snippet=policy_snippet,
            paid_mode=paid_zdr_mode,
            client=client,
            primary_model=primary_model,
        )

    gateway = OpenRouterGateway(api_key=api_key) if api_key is not None else get_default_gateway()
    raw_result = await gateway.track_b_chat_verify(
        collage_path=collage_path,
        audit_findings=audit_findings,
        policy_snippet=policy_snippet,
        paid_mode=paid_zdr_mode,
        client=client,
        primary_model=primary_model,
    )
    return raw_result
