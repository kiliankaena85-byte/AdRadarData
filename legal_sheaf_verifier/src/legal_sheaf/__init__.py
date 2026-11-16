"""
LegalSheafNet-16x41: Cellular Sheaf Neural Network for Russian Advertising Law Verification.
Strict Text & DOM Verification Engine under 38-FZ, 168-FZ, and Art. 14.3 KoAP RF.
"""

__version__ = "0.1.0"
__author__ = "Principal DL & Russian Legal Tech Researcher"

from legal_sheaf.schema import (
    SlotID,
    ParamIdx,
    LegalRegime,
    DocumentInput,
    VerificationReport,
    EdgeConflict,
    SlotDiagnostic,
)
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.it_accreditation import ITAccreditationAuditor

__all__ = [
    "SlotID",
    "ParamIdx",
    "LegalRegime",
    "DocumentInput",
    "VerificationReport",
    "EdgeConflict",
    "SlotDiagnostic",
    "KoAPSanctionsCalculator",
    "Compliance152FZScanner",
    "ITAccreditationAuditor",
]
