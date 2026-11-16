# -*- coding: utf-8 -*-
"""
Verification Engine Package.
Non-autoregressive verification, conformal grounding, and Sheaf-Vega verification gate.
"""

from .sheaf_vega_verifier import (
    VerifiedFactBundle,
    VegaUSensor,
    BackVerifierGate,
    fit_from_review_desk,
)

__all__ = [
    "VerifiedFactBundle",
    "VegaUSensor",
    "BackVerifierGate",
    "fit_from_review_desk",
]
