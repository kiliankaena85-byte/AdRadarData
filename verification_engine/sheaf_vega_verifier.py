# -*- coding: utf-8 -*-
"""
Double Non-Autoregressive Verification Gate:
LegalSheafNet-16x41 + Vega-U (Laya 0.4.32) + Strict Fact-Grounding Guard.

Implements the Cage for Generative Models:
1. Whitelist Citation & Article Lock: Rejects any hallucinated or unverified court cases/KoAP articles.
2. Context-Mismatch Lock: Rejects any claims of violations not present in the site's VerifiedFactBundle.
3. Conformal Grounding Sensor: Non-autoregressive entailment check (Vega-U) with conformal routing (auto vs review).
"""

from __future__ import annotations

import json
import logging
import os
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Set TensorFlow disabled before any laya import to prevent GPU backend lockups
os.environ["USE_TF"] = "0"

logger = logging.getLogger("verification_engine.sheaf_vega_verifier")

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "legal_sheaf_verifier" / "config"
CALIBRATION_DIR = Path(os.environ.get("ADRADAR_DATA_DIR", "C:\\AdRadarData")) / "models" / "vega_calibrated"


@dataclass
class VerifiedFactBundle:
    """
    Deterministic fact contract produced by LegalSheafNet-16x41,
    Chronotope, and certified live registry sensors (FNS/RKN/DOM).
    """
    domain: str
    legal_name: str
    inn: str
    allowed_articles: List[str]  # e.g. ["ч. 2 ст. 13.15 КоАП РФ", "ч. 1 ст. 20.3 КоАП РФ"]
    min_fine_rub: int = 0
    max_fine_rub: int = 0
    verified_court_cases: List[str] = field(default_factory=list)
    rkn_status: str = "UNVERIFIED"
    foreign_transfers: List[str] = field(default_factory=list)
    visual_disclaimers_found: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_summary_text(self) -> str:
        articles_str = ", ".join(self.allowed_articles) if self.allowed_articles else "Нет установленных нарушений"
        cases_str = ", ".join(self.verified_court_cases) if self.verified_court_cases else "Прецедентов не заявлено"
        disclaimers_str = ", ".join(self.visual_disclaimers_found) if self.visual_disclaimers_found else "Не обнаружены"
        return (
            f"ДОМЕН: {self.domain}\n"
            f"ОРГАНИЗАЦИЯ: {self.legal_name} (ИНН: {self.inn})\n"
            f"ПОДТВЕРЖДЕННЫЕ СТАТЬИ КОАП: {articles_str}\n"
            f"ПРЕДЕЛ ШТРАФОВ: {self.min_fine_rub} - {self.max_fine_rub} руб.\n"
            f"ПРОВЕРЕННЫЕ ДЕЛА: {cases_str}\n"
            f"НАЙДЕННЫЕ СНОСКИ/ДИСКЛЕЙМЕРЫ: {disclaimers_str}\n"
            f"РЕЕСТР РКН: {self.rkn_status}"
        )


def load_citations_whitelist() -> Tuple[Set[str], Set[str], Set[str]]:
    """Loads closed whitelist of verified court cases, KoAP articles, and federal laws."""
    wl_path = CONFIG_DIR / "verified_citations_whitelist.yaml"
    if not wl_path.exists():
        wl_path = Path.cwd() / "legal_sheaf_verifier" / "config" / "verified_citations_whitelist.yaml"
    if not wl_path.exists():
        return set(), set(), set()
    try:
        with open(wl_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cases = set(data.get("verified_court_cases", []))
        letters = set(data.get("verified_agency_letters", []))
        koap_direct = data.get("verified_koap_articles", [])
        koap_nested = data.get("verified_koap_and_laws", {}).get("koap_articles", [])
        laws_direct = data.get("verified_federal_laws", [])
        laws_nested = data.get("verified_koap_and_laws", {}).get("federal_laws", [])

        allowed_koap = set(str(x).strip() for x in (koap_direct + koap_nested))
        allowed_laws = set(str(x).strip() for x in (laws_direct + laws_nested))
        return (cases | letters), allowed_koap, allowed_laws
    except Exception as e:
        logger.warning("Failed to load citations whitelist: %s", e)
        return set(), set(), set()


class VegaUSensor:
    """
    Non-autoregressive verification sensor backed by Vega-U (Laya 0.4.32 / vegaml).
    Executes 'noul' (unary logic / entailment) and 'choice' questions with conformal routing.
    """

    def __init__(
        self,
        model_id: str = "nandakishorm/vega-U",
        light_mode: bool = False,
        device: Optional[str] = None,
    ):
        self.model_id = model_id
        self.light_mode = light_mode
        self.device = device
        self._agent = None

    def _get_agent(self):
        if self.light_mode:
            return None
        if self._agent is None:
            import laya
            logger.info("Initializing VegaUSensor agent (vegaml=True)...")
            self._agent = laya.Agent(vegaml=True)
        return self._agent

    def evaluate_claim(
        self,
        state: str,
        question_id: str,
        instructions: str,
        qtype: str = "noul",
        criteria: Optional[Union[Dict[str, str], List[str]]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates a single logical question over state using Vega-U.
        Questions format: {question_id: {"type": qtype, "instructions": instructions, "criteria": criteria}}
        """
        if self.light_mode:
            # Deterministic light mode for fast unit tests without loading 1.8GB model
            return {
                "route": "auto",
                "abstain": False,
                "unbound": False,
                "prediction_set": ["true"],
                "route_reason": "light_mode_simulated",
                "confidence": 0.99,
                "readout": "engine_simulated",
                "energy": -1.0,
                "settle_steps": 1,
            }

        agent = self._get_agent()
        q_def: Dict[str, Any] = {
            "type": qtype,
            "instructions": instructions,
        }
        if criteria is not None:
            q_def["criteria"] = criteria

        questions = {question_id: q_def}
        try:
            if len(state) > 12000:
                out = agent.predict_long(state=state, questions=questions)
            else:
                out = agent.predict(state=state, questions=questions)

            answers = out.get("answers", {})
            ans = answers.get(question_id, {})
            return {
                "route": ans.get("route", "review"),
                "abstain": ans.get("abstain", False),
                "unbound": ans.get("unbound", False),
                "prediction_set": ans.get("prediction_set", []),
                "route_reason": ans.get("route_reason", ""),
                "confidence": ans.get("confidence", 0.0),
                "readout": ans.get("readout", "engine"),
                "energy": ans.get("energy", 0.0),
                "settle_steps": ans.get("settle_steps", 0),
                "latency_ms": out.get("latency_ms", 0.0),
            }
        except Exception as e:
            logger.error("VegaUSensor prediction failed: %s", e)
            return {
                "route": "review",
                "abstain": True,
                "unbound": True,
                "prediction_set": [],
                "route_reason": f"Sensor error: {e}",
                "confidence": 0.0,
                "readout": "error",
                "energy": 0.0,
                "settle_steps": 0,
            }

    def evaluate_entailment(
        self,
        premise: str,
        hypothesis: str,
    ) -> Dict[str, Any]:
        """
        Checks whether hypothesis strictly follows from premise (no hallucinated claims).
        """
        state = f"PREMISE:\n{premise}\n\nHYPOTHESIS:\n{hypothesis}"
        instructions = (
            "Does the hypothesis contain only claims that are directly supported "
            "by the premise, without adding any unverified legal violations or facts?"
        )
        return self.evaluate_claim(
            state=state,
            question_id="entailment_grounding",
            instructions=instructions,
            qtype="noul",
        )


class BackVerifierGate:
    """
    Final Gate for Generative Proposals and Audits.
    Enforces strict citation whitelist, fact-bundle context lock, and Vega-U grounding.
    """

    def __init__(self, sensor: Optional[VegaUSensor] = None):
        self.sensor = sensor or VegaUSensor(light_mode=True)
        self.allowed_citations, self.allowed_koap, self.allowed_laws = load_citations_whitelist()

        # Regular expressions for detecting legal citations
        self.pat_soyu = re.compile(r"\b0?5-\d{3,5}/\d{2,4}/20\d{2}\b")
        self.pat_arbitr = re.compile(r"\b[АA]\d{2}-\d{3,7}/20\d{2}\b")
        self.pat_appeal = re.compile(r"\b\d{2}АП-\d{3,7}/20\d{2}\b")
        self.pat_letter = re.compile(r"\b(?:[А-ЯЁ]{2}|\d{2})/\d{4,6}/\d{2}\b")
        self.patterns = [self.pat_soyu, self.pat_arbitr, self.pat_appeal, self.pat_letter]

        # Pattern for extracting referenced KoAP articles
        self.pat_koap_num = re.compile(
            r"(?i)\b(?:ст\.|стать[еяию]\w*|ч\.\s*[\d,/-]+\s*ст\.)\s*(\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*(?:КоАП|Кодекса)"
        )
        self.pat_koap_bare = re.compile(r"(?i)\b(\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*КоАП")

    def extract_citations(self, text: str) -> List[str]:
        found = []
        for pat in self.patterns:
            for m in pat.finditer(text):
                found.append(m.group(0))
        return list(dict.fromkeys(found))

    def extract_koap_articles(self, text: str) -> List[str]:
        found = []
        for m in self.pat_koap_num.finditer(text):
            found.append(m.group(1))
        for m in self.pat_koap_bare.finditer(text):
            found.append(m.group(1))
        return list(dict.fromkeys(found))

    def verify_draft(
        self,
        draft_text: str,
        fact_bundle: VerifiedFactBundle,
        run_sensor: bool = True,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Verifies a generated text proposal or report against the VerifiedFactBundle.

        Returns:
            (is_verified, status_code, details_dict)
        """
        details: Dict[str, Any] = {
            "domain": fact_bundle.domain,
            "citations_found": [],
            "koap_articles_found": [],
            "violations": [],
        }

        # -------------------------------------------------------------
        # LAYER 1: Global Whitelist Check (Zero Hallucinated Citations/Articles)
        # -------------------------------------------------------------
        citations = self.extract_citations(draft_text)
        details["citations_found"] = citations
        for cit in citations:
            if cit not in self.allowed_citations:
                details["violations"].append(f"UNVERIFIED_CITATION: {cit}")
                return False, "BLOCKED_HALLUCINATED_CITATION", details

        koap_articles = self.extract_koap_articles(draft_text)
        details["koap_articles_found"] = koap_articles
        for art in koap_articles:
            if art not in self.allowed_koap:
                details["violations"].append(f"PROHIBITED_OR_UNVERIFIED_KOAP_ARTICLE: {art}")
                return False, "BLOCKED_UNVERIFIED_KOAP_ARTICLE", details

        # -------------------------------------------------------------
        # LAYER 2: Context-Mismatch Lock (Fact Bundle Scope Check)
        # -------------------------------------------------------------
        # Extract normalized numbers from fact_bundle.allowed_articles
        bundle_allowed_nums = set()
        for art_str in fact_bundle.allowed_articles:
            for m in re.finditer(r"\b(\d{1,2}\.\d{1,2}(?:\.\d+)?)\b", art_str):
                bundle_allowed_nums.add(m.group(1))

        # Check: Every article mentioned in the draft MUST be part of this site's bundle
        for art in koap_articles:
            if art not in bundle_allowed_nums:
                # Article is legal in Russia, but was NOT flagged on this domain!
                details["violations"].append(
                    f"CONTEXT_MISMATCH: Article {art} is cited in draft but absent from {fact_bundle.domain} fact bundle"
                )
                return False, "BLOCKED_CONTEXT_MISMATCH", details

        # -------------------------------------------------------------
        # LAYER 3: Vega-U Non-Autoregressive Grounding Entailment
        # -------------------------------------------------------------
        if run_sensor:
            premise = fact_bundle.to_summary_text()
            sensor_verdict = self.sensor.evaluate_entailment(premise=premise, hypothesis=draft_text)
            details["sensor_verdict"] = sensor_verdict

            if sensor_verdict.get("unbound", False):
                details["violations"].append("OOD_OUT_OF_DISTRIBUTION_DETECTED")
                return False, "BLOCKED_OOD_OUT_OF_DISTRIBUTION", details

            if sensor_verdict.get("abstain", False) or sensor_verdict.get("route") == "review":
                # Routed to human desk review rather than automated dispatch
                details["violations"].append("VEGA_ROUTED_TO_HUMAN_REVIEW")
                return False, "ROUTE_TO_REVIEW_DESK", details

            # In boolean noul questions, verify prediction set does not contain 'false'
            pset = sensor_verdict.get("prediction_set", [])
            if "false" in pset and "true" not in pset:
                details["violations"].append("UNGROUNDED_FACTUAL_CLAIMS")
                return False, "REJECTED_UNGROUNDED", details

        return True, "VERIFIED", details


def fit_from_review_desk(
    feedback_records: Union[List[Dict[str, Any]], Dict[str, Any], str, Path],
    output_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Saves reviewer decisions from review_desk.html to disk for continual calibration.
    Accepts:
    - List of feedback records
    - Single feedback dict (e.g. humanFeedback object from review_desk)
    - Path to a human_feedback.json file
    """
    target_dir = Path(output_dir) if output_dir else CALIBRATION_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    calib_file = target_dir / "calibration_dataset.jsonl"

    records: List[Dict[str, Any]] = []
    if isinstance(feedback_records, (str, Path)):
        p = Path(feedback_records)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    records = loaded
                elif isinstance(loaded, dict):
                    records = [loaded]
    elif isinstance(feedback_records, dict):
        records = [feedback_records]
    elif isinstance(feedback_records, list):
        records = feedback_records

    count = 0
    with open(calib_file, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

    logger.info("Saved %d feedback records to calibration dataset at %s", count, calib_file)
    return calib_file
