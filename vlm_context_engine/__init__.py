"""
AdRadar Multimodal Context Analysis Subsystem (vlm_context_engine).
Strict architectural isolation: zero dependencies inside legal_sheaf core.
"""

from __future__ import annotations

from vlm_context_engine.live_catalog import (
    LiveCatalogResolver,
    FinancialLockViolation,
)
from vlm_context_engine.trigger_cropper import (
    is_ssrf_dangerous_url,
    extract_policy_methods_snippet,
    capture_trigger_zones,
)
from vlm_context_engine.openrouter_gateway import (
    OpenRouterGateway,
    CircuitBreaker,
    TokenBucketRateLimiter,
    VlmSqliteCache,
    sanitize_model_citations,
    verify_visual_triggers,
)
from vlm_context_engine.reconciler import ContextRuleReconciler
from vlm_context_engine.review_desk import (
    ReviewDeskBuilder,
    generate_review_desk_html,
    map_finding_to_zone,
    build_commercial_proposal_text,
)

__version__ = "2.0.0"

__all__ = [
    "LiveCatalogResolver",
    "FinancialLockViolation",
    "is_ssrf_dangerous_url",
    "extract_policy_methods_snippet",
    "capture_trigger_zones",
    "OpenRouterGateway",
    "CircuitBreaker",
    "TokenBucketRateLimiter",
    "VlmSqliteCache",
    "sanitize_model_citations",
    "verify_visual_triggers",
    "ContextRuleReconciler",
    "ReviewDeskBuilder",
    "generate_review_desk_html",
    "map_finding_to_zone",
    "build_commercial_proposal_text",
]
