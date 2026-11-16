"""
Legal Precedent Index for LegalSheafNet-16x41.
Vector matching against Russian legal corpus: 38-FZ, 168-FZ, KoAP RF,
FAS Russia letters/warnings, and enacted Court/UFAS rulings.
Computes Block C parameters [20..35] across all 16 legal slots.
Supports both deepvk/USER2-base in FP16 on CUDA and deterministic fast n-gram fallback.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch

from legal_sheaf.chronotope import (
    AUTHORITY_TIER_WEIGHTS,
    JurisdictionRouter,
    TemporalValidityGate,
)
from legal_sheaf.config import MODELS_CACHE_DIR
from legal_sheaf.schema import ParamIdx, SlotID


class LegalPrecedentIndex:
    """
    Vector search index and legal feature builder for parameters [20..35].
    Supports:
    - Full-Semantic mode: deepvk/USER2-base bi-encoder in FP16 on CUDA (~300 MB VRAM)
    - Fast-Standalone mode: deterministic subword n-gram hashing for offline testing
    """

    def __init__(
        self,
        seed_dir: Optional[str] = None,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        use_fast_ngram_fallback: bool = False,
        offline_ngram_mode: Optional[bool] = None,
    ):
        self.device = torch.device(device if (device == "cuda" and torch.cuda.is_available()) else "cpu")
        if offline_ngram_mode is not None:
            self.use_fast_ngram_fallback = offline_ngram_mode
        else:
            self.use_fast_ngram_fallback = use_fast_ngram_fallback
        self.encoder = None

        if seed_dir is None:
            # Default to legal_kb_seed directory relative to package root
            pkg_root = Path(__file__).resolve().parent.parent.parent
            seed_dir = str(pkg_root / "legal_kb_seed")

        self.seed_dir = Path(seed_dir)
        self.jurisdiction_router = JurisdictionRouter()
        self.temporal_gate = TemporalValidityGate()
        self.statutes: List[Dict[str, Any]] = []
        self.fas_letters: List[Dict[str, Any]] = []
        self.court_cases: List[Dict[str, Any]] = []
        self.all_precedents: List[Dict[str, Any]] = []

        self._load_seed_corpus()

        # Initialize deepvk/USER2-base encoder if not in fast fallback mode
        if not self.use_fast_ngram_fallback:
            try:
                os.environ["HF_HOME"] = str(MODELS_CACHE_DIR)
                os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(MODELS_CACHE_DIR)
                from sentence_transformers import SentenceTransformer
                self.encoder = SentenceTransformer("deepvk/USER2-base", device=str(self.device))
                if self.device.type == "cuda":
                    self.encoder.half()  # Run in FP16 on RTX 4060 (~300 MB VRAM)
            except Exception as e:
                # Fallback to n-gram hashing if offline or model cannot be loaded
                self.use_fast_ngram_fallback = True

        self._build_vector_index()

    @property
    def offline_ngram_mode(self) -> bool:
        return self.use_fast_ngram_fallback

    @offline_ngram_mode.setter
    def offline_ngram_mode(self, val: bool):
        self.use_fast_ngram_fallback = val
        self._build_vector_index()

    @property
    def embeddings(self) -> torch.Tensor:
        """Alias for torch_precedent_matrix."""
        return self.torch_precedent_matrix

    @embeddings.setter
    def embeddings(self, val: torch.Tensor):
        self.torch_precedent_matrix = val
        self.precedent_matrix = val.detach().cpu().numpy()

    def _load_seed_corpus(self):
        """Load JSONL corpus files."""
        statutes_path = self.seed_dir / "statutes_rf.jsonl"
        fas_path = self.seed_dir / "fas_letters_warnings.jsonl"
        courts_path = self.seed_dir / "court_and_ufas_cases.jsonl"

        if statutes_path.exists():
            with open(statutes_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.statutes.append(json.loads(line))

        if fas_path.exists():
            with open(fas_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.fas_letters.append(json.loads(line))

        if courts_path.exists():
            with open(courts_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.court_cases.append(json.loads(line))

        # Combined precedents list
        for item in self.statutes:
            item["_type"] = "statute"
            item["_is_guilty"] = not item.get("is_exemption", False)
            item["_is_safe"] = item.get("is_exemption", False)
            item["_search_text"] = f"{item.get('title', '')} {item.get('text', '')}"
            self.all_precedents.append(item)

        for item in self.fas_letters:
            item["_type"] = "fas_letter"
            item["_is_safe"] = item.get("is_safe_harbor", False)
            item["_is_guilty"] = not item.get("is_safe_harbor", False)
            item["_search_text"] = f"{item.get('title', '')} {item.get('text', '')}"
            self.all_precedents.append(item)

        for item in self.court_cases:
            item["_type"] = "court_case"
            status = item.get("procedural_status", "IN_FORCE")
            if status == "OVERTURNED":
                # Decision overturned by higher instance: inverts guilty ruling into a safe precedent!
                item["_is_guilty"] = False
                item["_is_safe"] = True
                item["verdict"] = "SAFE"
            else:
                item["_is_guilty"] = item.get("verdict") == "GUILTY"
                item["_is_safe"] = item.get("verdict") == "SAFE"
            item["_search_text"] = f"{item.get('summary', '')} {item.get('sample_fragment', '')}"
            self.all_precedents.append(item)

    def _compute_ngram_hash_vector(self, text: str, dim: int = 256) -> np.ndarray:
        """
        Deterministic, ultrafast subword n-gram hash embedding (< 0.05 ms).
        Extracts char 3-5-grams and word tokens, projecting to unit hypersphere.
        """
        vec = np.zeros(dim, dtype=np.float32)
        text_lower = text.lower()
        words = re.findall(r"\w+", text_lower)
        
        # Word-level features
        for w in words:
            h = hash(w) % dim
            sign = 1.0 if (hash(w + "_s") % 2 == 0) else -1.0
            vec[h] += sign * (1.0 + math.log1p(len(w)))

        # Character 3-gram and 4-gram features
        for n in (3, 4):
            for i in range(len(text_lower) - n + 1):
                gram = text_lower[i : i + n]
                h = hash(gram) % dim
                sign = 1.0 if (hash(gram + "_s") % 2 == 0) else -1.0
                vec[h] += sign * 0.5

        norm = np.linalg.norm(vec)
        if norm > 1e-7:
            vec /= norm
        return vec

    def _build_vector_index(self):
        """Precompute embeddings for all precedents in corpus."""
        if not self.all_precedents:
            return

        if not self.use_fast_ngram_fallback and self.encoder is not None:
            texts = [prec["_search_text"] for prec in self.all_precedents]
            embeds = self.encoder.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                batch_size=32,
            )
            self.precedent_matrix = embeds.astype(np.float32)
        else:
            embeds = []
            for prec in self.all_precedents:
                text = prec["_search_text"]
                vec = self._compute_ngram_hash_vector(text, dim=256)
                embeds.append(vec)
            self.precedent_matrix = np.stack(embeds, axis=0)  # [N, dim]

        self.torch_precedent_matrix = torch.from_numpy(self.precedent_matrix).to(self.device)

    def _calculate_recency(self, date_str: Optional[str]) -> float:
        """Calculate exp(-months_age / 24.0) relative to reference date."""
        if not date_str:
            return 0.75
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            ref_date = datetime(2025, 4, 1)
            months = max(0.0, (ref_date - d).days / 30.4375)
            return float(math.exp(-months / 24.0))
        except Exception:
            return 0.75

    def match_slot_precedents(
        self,
        slot_text: str,
        slot_id: int,
        precomputed_vec: Optional[np.ndarray] = None,
        target_inn: Optional[str] = None,
    ) -> Dict[int, float]:
        """
        Compute the 16 parameters [20..35] for a given slot.
        Applies geographic relevance weighting and court/UFAS authority tiers.
        """
        results: Dict[int, float] = {i: 0.0 for i in range(20, 36)}

        if not slot_text.strip() or len(self.all_precedents) == 0:
            return results

        if precomputed_vec is not None:
            q_vec = precomputed_vec
        elif not self.use_fast_ngram_fallback and self.encoder is not None:
            q_vec = self.encoder.encode(
                [slot_text], convert_to_numpy=True, normalize_embeddings=True
            )[0].astype(np.float32)
        else:
            q_vec = self._compute_ngram_hash_vector(slot_text, dim=256)

        sims = np.dot(self.precedent_matrix, q_vec)  # [N]
        sims_clamped = np.clip((sims + 1.0) / 2.0, 0.0, 1.0)

        # Resolve regional jurisdiction for geographic weighting
        target_region, ufas_name, target_circuit = self.jurisdiction_router.resolve_jurisdiction(target_inn)

        # 1. Statute-specific metrics (params 20..23)
        statute_matches = []
        exemption_sims = []
        for i, prec in enumerate(self.all_precedents):
            if prec["_type"] == "statute":
                if slot_id in prec.get("target_slots", []):
                    statute_matches.append((sims_clamped[i], prec))
                if prec.get("is_exemption", False):
                    exemption_sims.append(sims_clamped[i])

        if statute_matches:
            statute_matches.sort(key=lambda x: x[0], reverse=True)
            best_sim, best_statute = statute_matches[0]
            results[20] = float(best_sim)
            results[21] = float(best_statute.get("imperative_weight", 0.8))
            results[22] = float(best_statute.get("koap_severity", 0.5))
        else:
            results[20] = 0.1
            results[21] = 0.5
            results[22] = 0.3

        results[23] = float(max(exemption_sims)) if exemption_sims else 0.0

        # 2. Guilty case / warning metrics (params 24..27) & safe matches (28..31)
        guilty_matches = []
        safe_matches = []
        all_scored = []

        for i, prec in enumerate(self.all_precedents):
            base_sim = float(sims_clamped[i])
            prec_tier = prec.get("authority_tier", "REGIONAL_UFAS")
            prec_jurisdiction = prec.get("jurisdiction_scope", "FEDERAL")
            geo_mult = self.jurisdiction_router.calculate_geographic_weight(
                prec_jurisdiction, target_region, target_circuit, prec_tier
            )
            sim = float(np.clip(base_sim * geo_mult, 0.0, 1.0))

            all_scored.append((sim, prec))
            if prec["_is_guilty"]:
                guilty_matches.append((sim, prec))
            if prec["_is_safe"]:
                safe_matches.append((sim, prec))

        all_scored.sort(key=lambda x: x[0], reverse=True)
        guilty_matches.sort(key=lambda x: x[0], reverse=True)
        safe_matches.sort(key=lambda x: x[0], reverse=True)

        if guilty_matches:
            g_best_sim, g_best_prec = guilty_matches[0]
            results[24] = float(g_best_sim)
            top3_g = [s for s, _ in guilty_matches[:3]]
            results[25] = float(np.mean(top3_g))
            results[26] = float(g_best_prec.get("authority_weight", 0.75))
            results[27] = float(self._calculate_recency(g_best_prec.get("date")))
        else:
            results[24] = 0.0
            results[25] = 0.0
            results[26] = 0.5
            results[27] = 0.7

        # 3. Safe precedents metrics (params 28..31)
        if safe_matches:
            s_best_sim, s_best_prec = safe_matches[0]
            results[28] = float(s_best_sim)
            top3_s = [s for s, _ in safe_matches[:3]]
            results[29] = float(np.mean(top3_s))
            results[30] = float(s_best_prec.get("authority_weight", 0.85))
            results[31] = float(self._calculate_recency(s_best_prec.get("date")))
        else:
            results[28] = 0.0
            results[29] = 0.0
            results[30] = 0.5
            results[31] = 0.7

        # 4. Differential balance & controversy (params 32..35)
        # 32: Net margin in [-1.0, 1.0]
        results[32] = float(np.clip(results[24] - results[28], -1.0, 1.0))

        # 33: Neighbor density (proportion of top-10 with sim >= 0.65)
        top10 = all_scored[:10]
        dense_count = sum(1 for s, _ in top10 if s >= 0.65)
        results[33] = float(dense_count / max(1, len(top10)))

        # 34: Practice controversy entropy over top 7 precedents
        top7 = all_scored[:7]
        guilty_count = sum(1 for _, p in top7 if p["_is_guilty"])
        total_top7 = max(1, len(top7))
        p_guilty = guilty_count / total_top7
        if 0.0 < p_guilty < 1.0:
            entropy = -p_guilty * math.log2(p_guilty) - (1.0 - p_guilty) * math.log2(1.0 - p_guilty)
        else:
            entropy = 0.0
        results[34] = float(np.clip(entropy, 0.0, 1.0))

        # 35: Warning / 4.1.1 precedent ratio in top 10
        warning_count = sum(1 for _, p in top10 if p.get("is_warning", False) or p.get("warning_ratio", 0.0) > 0.5)
        results[35] = float(warning_count / max(1, len(top10)))

        return results

    def compute_all_slots_block_c(
        self,
        slot_texts: Dict[int, str],
        target_inn: Optional[str] = None,
    ) -> np.ndarray:
        """
        Compute Block C features (16 slots x 16 parameters) -> np.ndarray of shape [16, 16].
        Optimized with batched encoding when using deepvk/USER2-base and regional jurisdiction weighting.
        """
        out = np.zeros((16, 16), dtype=np.float32)
        texts_list = [slot_texts.get(s_id, "") for s_id in range(16)]

        # Precompute slot embeddings in 1 single GPU batch if using deep encoder
        if not self.use_fast_ngram_fallback and self.encoder is not None:
            q_vecs = self.encoder.encode(
                texts_list,
                convert_to_numpy=True,
                normalize_embeddings=True,
                batch_size=16,
            ).astype(np.float32)
        else:
            q_vecs = [self._compute_ngram_hash_vector(t, dim=256) for t in texts_list]

        for s_id in range(16):
            param_dict = self.match_slot_precedents(
                texts_list[s_id],
                s_id,
                precomputed_vec=q_vecs[s_id],
                target_inn=target_inn,
            )
            for p_idx in range(20, 36):
                out[s_id, p_idx - 20] = param_dict[p_idx]
        return out

    def check_regional_divergence(
        self,
        slot_text: str,
        target_inn: Optional[str] = None,
        slot_id: int = SlotID.LINK_TOPOLOGY,
    ) -> Tuple[bool, Optional[str], float, float]:
        """
        Calculates local circuit margin vs federal/Moscow benchmark margin.
        If sign(local_margin) != sign(federal_margin), returns (True, explanation, local_margin, federal_margin).
        """
        if not target_inn:
            return False, None, 0.0, 0.0

        target_region, ufas_name, target_circuit = self.jurisdiction_router.resolve_jurisdiction(target_inn)

        # 1. Local circuit evaluation
        local_scored = self.match_slot_precedents(slot_text, slot_id=slot_id, target_inn=target_inn)
        local_margin = local_scored[32]  # Param 32 is PRECEDENT_NET_MARGIN

        # 2. Moscow / Federal benchmark evaluation (with Moscow INN 77...)
        fed_scored = self.match_slot_precedents(slot_text, slot_id=slot_id, target_inn="7707083893")
        federal_margin = fed_scored[32]

        is_div, explanation = self.jurisdiction_router.detect_practice_divergence(
            local_margin, federal_margin, target_circuit, ufas_name
        )
        return is_div, explanation, local_margin, federal_margin
