"""
VLM Context Engine: Direct Google Gemini API Gateway (Track A & Track B).
Calls https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
with x-goog-api-key header, structured JSON output (responseMimeType: application/json),
dynamic YAML model catalog, Token-Bucket RateLimiter, CircuitBreaker, SQLite cache,
and Citation Whitelist Sanitizer.
Strictly Zero-Vision-Library isolation (NO cv2, NO PIL, NO google-genai SDK).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import httpx
import yaml

from legal_sheaf import config as legal_config
from vlm_context_engine.openrouter_gateway import (
    CircuitBreaker,
    TokenBucketRateLimiter,
    VlmSqliteCache,
    _mask_api_key,
    _parse_api_key_list,
    _parse_json_resilient,
    _read_env_key,
    load_citation_whitelist,
    sanitize_model_citations,
)

logger = logging.getLogger("vlm_context_engine.gemini_gateway")

ROOT_DIR = Path(__file__).resolve().parent.parent
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS_ENDPOINT = f"{GEMINI_API_BASE}/models"
GEMINI_PROMPT_VERSION = "2026.11-gemini-direct-v1"


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    return Path(legal_config.ADRADAR_DATA_DIR).resolve()


# =============================================================================
# 1. API KEY RESOLUTION & MODEL CATALOG
# =============================================================================
def get_gemini_api_keys() -> List[str]:
    """
    Retrieves Google Gemini API key pool from environment or ADRADAR_DATA_DIR/.env / repo .env / CWD/.env.
    Supports GEMINI_API_KEY, GEMINI_API_KEYS, and GOOGLE_API_KEY.
    """
    merged: List[str] = []
    for var_name in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY"):
        for k in _parse_api_key_list(os.environ.get(var_name, "")):
            if k not in merged:
                merged.append(k)
    if merged:
        return merged

    candidates = [
        _active_data_dir() / ".env",
        ROOT_DIR / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        local_merged: List[str] = []
        for var_name in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY"):
            raw_val = _read_env_key(env_path, var_name)
            for k in _parse_api_key_list(raw_val):
                if k not in local_merged:
                    local_merged.append(k)
        if local_merged:
            return local_merged

    return []


def get_gemini_api_key() -> str:
    """Retrieves primary GEMINI_API_KEY / GOOGLE_API_KEY."""
    keys = get_gemini_api_keys()
    return keys[0] if keys else ""


def extract_gemini_response_text(payload: Dict[str, Any]) -> str:
    """
    Extracts model answer text from Gemini generateContent JSON, skipping thinking parts
    (`thought: true`) unless no non-thought text part was returned.
    """
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    regular_parts: List[str] = []
    fallback_parts: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        txt = part.get("text")
        if not isinstance(txt, str) or not txt.strip():
            continue
        fallback_parts.append(txt)
        if not part.get("thought"):
            regular_parts.append(txt)
    if regular_parts:
        return "\n".join(regular_parts).strip()
    return "\n".join(fallback_parts).strip()


class GeminiCatalogResolver:
    """
    Loads model configuration from legal_sheaf_verifier/config/gemini_models.yaml
    and optionally validates live models via GET /v1beta/models (24h cache).
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self._find_config()
        self.config = self._load_config()
        settings = self.config.get("live_catalog_settings", {})
        self.refresh_interval = settings.get("refresh_interval_seconds", 86400)
        self.catalog_endpoint = settings.get("catalog_endpoint", GEMINI_MODELS_ENDPOINT)
        active_data_dir = _active_data_dir()
        raw_cache_file = str(settings.get("cache_file") or "").replace("\\", "/")
        if raw_cache_file.lower().startswith("c:/adradardata/"):
            rel_part = raw_cache_file[len("c:/adradardata/"):]
            self.cache_file = active_data_dir / rel_part
        elif raw_cache_file:
            self.cache_file = Path(raw_cache_file)
        else:
            self.cache_file = active_data_dir / "cache" / "gemini_models_catalog.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _find_config() -> Path:
        candidates = [
            Path.cwd() / "legal_sheaf_verifier" / "config" / "gemini_models.yaml",
            ROOT_DIR / "legal_sheaf_verifier" / "config" / "gemini_models.yaml",
            Path.cwd() / "config" / "gemini_models.yaml",
            ROOT_DIR / "config" / "gemini_models.yaml",
        ]
        for p in candidates:
            if p.exists():
                return p
        return ROOT_DIR / "legal_sheaf_verifier" / "config" / "gemini_models.yaml"

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            try:
                return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                logger.warning(f"Failed to parse gemini_models.yaml: {exc}")
        return {}

    async def get_live_catalog(
        self,
        api_key: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Loads 24h cached Gemini models catalog or queries GET /v1beta/models."""
        if self.cache_file.exists():
            mtime = self.cache_file.stat().st_mtime
            if (time.time() - mtime) < self.refresh_interval:
                try:
                    data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    models = data.get("models") or []
                    return {
                        str(m.get("name", "")).removeprefix("models/"): m
                        for m in models
                        if isinstance(m, dict) and m.get("name")
                    }
                except Exception:
                    pass

        if not api_key:
            return {}

        close_local = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=15.0)
            close_local = True
        try:
            resp = await c.get(
                self.catalog_endpoint,
                headers={"x-goog-api-key": api_key, "User-Agent": "AdRadar-GeminiGateway/1.0"},
            )
            if resp.status_code == 200:
                raw_json = resp.json()
                self.cache_file.write_text(json.dumps(raw_json, ensure_ascii=False, indent=2), encoding="utf-8")
                models = raw_json.get("models") or []
                return {
                    str(m.get("name", "")).removeprefix("models/"): m
                    for m in models
                    if isinstance(m, dict) and m.get("name")
                }
        except Exception as exc:
            logger.warning(f"Could not refresh Gemini live catalog: {exc}")
        finally:
            if close_local:
                await c.aclose()

        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                models = data.get("models") or []
                return {
                    str(m.get("name", "")).removeprefix("models/"): m
                    for m in models
                    if isinstance(m, dict) and m.get("name")
                }
            except Exception:
                pass
        return {}

    async def resolve_models(
        self,
        vip_mode: bool = False,
        api_key: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[str]:
        """Returns prioritized non-banned Gemini model IDs from gemini_models.yaml."""
        vlm_cfg = self.config.get("vlm_visual_qc") or {}
        primary = str(vlm_cfg.get("primary") or "").strip()
        fallbacks = [str(m).strip() for m in (vlm_cfg.get("fallbacks") or []) if str(m).strip()]
        vip_arbiter = str(vlm_cfg.get("vip_arbiter") or "").strip()
        banned = {str(b).strip().removeprefix("models/") for b in (self.config.get("banned_models") or [])}

        raw_candidates: List[str] = []
        if vip_mode and vip_arbiter:
            raw_candidates.append(vip_arbiter)
        if primary:
            raw_candidates.append(primary)
        raw_candidates.extend(fallbacks)

        deduped: List[str] = []
        for raw_m in raw_candidates:
            m = raw_m.removeprefix("models/")
            if not m or m in banned or m in deduped:
                continue
            deduped.append(m)

        catalog = await self.get_live_catalog(api_key=api_key, client=client)
        if not catalog:
            return deduped

        verified = [
            m for m in deduped
            if m in catalog and (
                not catalog[m].get("supportedGenerationMethods")
                or "generateContent" in (catalog[m].get("supportedGenerationMethods") or [])
            )
        ]
        return verified or deduped


# =============================================================================
# 2. DIRECT GEMINI GATEWAY (Track A & Track B)
# =============================================================================
class GeminiGateway:
    """
    Direct Google Gemini API gateway for Track A decisions and Track B visual verification.
    Calls POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    with native inline_data vision payloads and structured application/json responses.
    """

    def __init__(
        self,
        api_key: Optional[Union[str, List[str]]] = None,
        catalog_resolver: Optional[GeminiCatalogResolver] = None,
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
            resolved_keys = get_gemini_api_keys()

        self.api_keys: List[str] = resolved_keys
        self.api_key: str = resolved_keys[0] if resolved_keys else ""
        self._key_cursor: int = 0
        self._exhausted_keys: Set[str] = set()
        self.catalog_resolver = catalog_resolver or GeminiCatalogResolver()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(max_rpm=30)
        self.cache = cache or VlmSqliteCache()
        self.allowed_citations, self.allowed_koap = load_citation_whitelist()

    def _active_key_pool(self) -> List[str]:
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
            pool = self._active_key_pool()
            if pool:
                self.api_key = pool[0]

    @staticmethod
    def _build_headers(api_key: str) -> Dict[str, str]:
        return {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_request_body(
        system_instruction: str,
        user_text: str,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/png",
        model_id: str = "",
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = [{"text": user_text}]
        if image_bytes:
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            })

        gen_config: Dict[str, Any] = {"temperature": temperature}
        if not model_id.lower().removeprefix("models/").startswith("gemma-"):
            gen_config["responseMimeType"] = "application/json"

        return {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_config,
        }

    async def track_a_decide(
        self,
        instructions: str,
        context_data: Dict[str, Any],
        decision_questions: List[Dict[str, Any]],
        image_bytes: Optional[bytes] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        """Executes structured decision arbitration via Direct Gemini API."""
        active_keys = self._active_key_pool()
        if not active_keys:
            return {
                "status": "UNVERIFIED",
                "decisions": [],
                "error": "GEMINI_API_KEY is not set (Zero-Mock active).",
            }

        cache_bytes = image_bytes or b""
        cache_key = self.cache.compute_key(cache_bytes, decision_questions, f"{GEMINI_PROMPT_VERSION}_track_a")
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        system_prompt = "Ты — беспристрастный арбитр правовых норм и фактических доказательств."
        user_prompt = (
            f"Инструкция арбитража:\n{instructions}\n\n"
            f"Контекст:\n<untrusted_page_data>\n{json.dumps(context_data, ensure_ascii=False, indent=2)}\n</untrusted_page_data>\n\n"
            f"Вопросы для решения:\n<untrusted_page_data>\n{json.dumps(decision_questions, ensure_ascii=False, indent=2)}\n</untrusted_page_data>\n\n"
            "Вынеси обоснованное решение по каждому вопросу строго в формате JSON:\n"
            "{\n"
            '  "status": "DECIDED",\n'
            '  "decisions": [\n'
            '    {"question_id": "...", "answer": "YES|NO|UNCLEAR", "justification_ru": "...", "confidence": 0.0}\n'
            "  ]\n"
            "}"
        )

        close_client = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=35.0)
            close_client = True

        try:
            models = await self.catalog_resolver.resolve_models(
                vip_mode=False, api_key=active_keys[0], client=c
            )
            available = [m for m in models if self.circuit_breaker.is_available(m)]
            for model in available:
                pool = self._active_key_pool()
                if not pool:
                    break
                req_body = self._build_request_body(
                    system_instruction=system_prompt,
                    user_text=user_prompt,
                    image_bytes=image_bytes,
                    mime_type="image/png",
                    model_id=model,
                )
                for active_key in pool:
                    await self.rate_limiter.acquire()
                    endpoint = f"{GEMINI_API_BASE}/models/{model}:generateContent"
                    try:
                        resp = await c.post(endpoint, json=req_body, headers=self._build_headers(active_key))
                        if resp.status_code == 200:
                            raw_txt = extract_gemini_response_text(resp.json())
                            parsed = _parse_json_resilient(raw_txt)
                            sanitized = sanitize_model_citations(parsed, self.allowed_citations, self.allowed_koap)
                            if isinstance(sanitized, dict):
                                parsed = sanitized
                            self._rotate_past_key(active_key)
                            self.circuit_breaker.record_success(model)
                            self.cache.set(cache_key, parsed, model)
                            return parsed
                        elif resp.status_code in (401, 403):
                            self._exhausted_keys.add(active_key)
                            self._rotate_past_key(active_key)
                            continue
                        elif resp.status_code == 429:
                            self._rotate_past_key(active_key)
                            continue
                        else:
                            if resp.status_code in (500, 503):
                                self.circuit_breaker.record_failure(model)
                            break
                    except Exception as exc:
                        self.circuit_breaker.record_failure(model)
                        logger.warning(f"Gemini Track A error on {model}: {exc}")
                        break
        finally:
            if close_client:
                await c.aclose()

        return {"status": "UNVERIFIED", "decisions": [], "error": "Gemini Track A arbitration failed."}

    async def track_b_chat_verify(
        self,
        collage_path: Union[str, Path],
        audit_findings: List[Dict[str, Any]],
        policy_snippet: Optional[str] = None,
        paid_mode: bool = False,
        client: Optional[httpx.AsyncClient] = None,
        primary_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes multimodal visual verification of 3 trigger zones using Direct Gemini Vision API."""
        if not self._active_key_pool():
            logger.info("GEMINI_API_KEY is not set. Skipping Gemini VLM verification (Zero-Mock active).")
            return {
                "status": "VLM_UNVERIFIED_NO_KEY",
                "confirmed": None,
                "readability_ok": None,
                "details": "GEMINI_API_KEY не задан. Визуальная верификация VLM пропущена (требуется ручной просмотр в review_desk).",
                "model_used": None,
                "findings_verdicts": [],
                "methods_mismatch_note": "",
                "human_hint_ru": "Ключ Gemini API не настроен. Проверьте скриншоты зон вручную в интерфейсе review_desk.",
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
        cache_key = self.cache.compute_key(collage_bytes, audit_findings, GEMINI_PROMPT_VERSION)
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"Gemini VLM cache hit for key {cache_key[:12]}...")
            return cached

        mime_type = (
            "image/jpeg"
            if (str(c_path).lower().endswith((".jpg", ".jpeg")) or collage_bytes.startswith(b"\xff\xd8\xff"))
            else "image/png"
        )

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
            '      "confidence": 0.0\n'
            "    }\n"
            "  ],\n"
            '  "page_genre": "COMMERCIAL_STORE" | "PORTFOLIO_CASE" | "HR_VACANCY" | "B2B_CORPORATE_ONLY" | "INFORMATIONAL",\n'
            '  "methods_mismatch_note": "...",\n'
            '  "human_hint_ru": "..."\n'
            "}"
        )

        safe_findings_json = json.dumps(findings_summary, ensure_ascii=False, indent=2)
        safe_policy_text = policy_snippet or "Не предоставлен"
        user_text = (
            f"Список нарушений от сканера:\n<untrusted_page_data>\n{safe_findings_json}\n</untrusted_page_data>\n\n"
            f"Фрагмент методов из Политики (PII-очищен):\n<untrusted_page_data>\n{safe_policy_text}\n</untrusted_page_data>\n\n"
            "Изучи приложенный коллаж скриншотов зон #1, #2, #3 и вынеси судебное заключение по каждому пункту."
        )

        close_client = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=45.0)
            close_client = True

        result_data: Optional[Dict[str, Any]] = None
        used_model: Optional[str] = None
        last_error = ""

        try:
            pool_first = self._active_key_pool()[0]
            try:
                resolved_models = await self.catalog_resolver.resolve_models(
                    vip_mode=paid_mode, api_key=pool_first, client=c
                )
            except Exception as exc:
                logger.warning(f"Gemini catalog resolution error: {exc}")
                resolved_models = []

            clean_primary = str(primary_model or "").strip().removeprefix("models/").removeprefix("google/")
            if clean_primary:
                resolved_models = [clean_primary] + [m for m in resolved_models if m != clean_primary]

            available = [m for m in resolved_models if self.circuit_breaker.is_available(m)]
            if not available:
                return {
                    "status": "UNVERIFIED",
                    "confirmed": None,
                    "readability_ok": None,
                    "details": "Все модели Gemini VLM временно недоступны (CircuitBreaker или пустой каталог).",
                    "model_used": None,
                    "findings_verdicts": [],
                    "methods_mismatch_note": "",
                    "human_hint_ru": "Сервис VLM временно недоступен. Проверьте скриншоты вручную.",
                }

            for model in available:
                keys_for_model = self._active_key_pool()
                if not keys_for_model:
                    last_error = "Все ключи Gemini API исчерпаны или отклонены."
                    break

                req_body = self._build_request_body(
                    system_instruction=system_instruction,
                    user_text=user_text,
                    image_bytes=collage_bytes,
                    mime_type=mime_type,
                    model_id=model,
                )
                endpoint = f"{GEMINI_API_BASE}/models/{model}:generateContent"
                model_rate_limited_all_keys = True

                for active_key in keys_for_model:
                    await self.rate_limiter.acquire()
                    try:
                        resp = await c.post(endpoint, json=req_body, headers=self._build_headers(active_key))
                        if resp.status_code == 200:
                            raw_content = extract_gemini_response_text(resp.json())
                            try:
                                result_data = _parse_json_resilient(raw_content)
                                used_model = model
                                model_rate_limited_all_keys = False
                                self._rotate_past_key(active_key)
                                self.circuit_breaker.record_success(model)
                                break
                            except Exception as parse_err:
                                last_error = f"JSON parse error from {model}: {parse_err}"
                                model_rate_limited_all_keys = False
                                self.circuit_breaker.record_failure(model)
                                break
                        elif resp.status_code in (401, 403):
                            last_error = f"HTTP {resp.status_code} on key {_mask_api_key(active_key)}"
                            self._exhausted_keys.add(active_key)
                            self._rotate_past_key(active_key)
                            continue
                        elif resp.status_code == 429:
                            last_error = f"HTTP 429 from {model} on key {_mask_api_key(active_key)}"
                            self._rotate_past_key(active_key)
                            continue
                        else:
                            last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                            model_rate_limited_all_keys = False
                            if resp.status_code in (500, 503):
                                self.circuit_breaker.record_failure(model)
                            break
                    except Exception as conn_err:
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
                "details": f"Не удалось получить ответ Gemini VLM: {last_error}",
                "model_used": None,
                "findings_verdicts": [],
                "methods_mismatch_note": "",
                "human_hint_ru": "Произошла ошибка при обращении к Gemini VLM. Проверьте скриншоты зон вручную в review_desk.",
            }

        sanitized_result = sanitize_model_citations(
            result_data, self.allowed_citations, self.allowed_koap
        )
        if isinstance(sanitized_result, dict):
            result_data = sanitized_result

        result_data["model_used"] = used_model
        self.cache.set(cache_key, result_data, used_model or "unknown")
        return result_data


_DEFAULT_GEMINI_GATEWAY: Optional[GeminiGateway] = None
_DEFAULT_GEMINI_GATEWAY_DIR: Optional[Path] = None


def get_default_gemini_gateway() -> GeminiGateway:
    """Returns a lazy singleton GeminiGateway bound to the active ADRADAR_DATA_DIR."""
    global _DEFAULT_GEMINI_GATEWAY, _DEFAULT_GEMINI_GATEWAY_DIR
    cur_dir = _active_data_dir()
    if (
        _DEFAULT_GEMINI_GATEWAY is None
        or _DEFAULT_GEMINI_GATEWAY_DIR != cur_dir
        or not _DEFAULT_GEMINI_GATEWAY._active_key_pool()
    ):
        _DEFAULT_GEMINI_GATEWAY = GeminiGateway()
        _DEFAULT_GEMINI_GATEWAY_DIR = cur_dir
    return _DEFAULT_GEMINI_GATEWAY
