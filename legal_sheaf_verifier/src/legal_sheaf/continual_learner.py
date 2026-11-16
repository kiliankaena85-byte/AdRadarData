"""
Continual Learning Engine and Champion/Challenger Gate for LegalSheafNet.
Implements:
1. Zero-Retrain Law Ingestion (Hot-swap of legal norms and FAS letters via vector index).
2. Population Stability Index (PSI) Feature Drift Detector across 16 slots.
3. Experience Replay Buffer (75% Invariant Golden Archetypes + 25% New Feedback).
4. L2-SP Regularization against Champion parameters to prevent catastrophic forgetting.
5. Promotion Gate enforcing Precision >= 0.96 and ECE <= 0.05 before deployment.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.calibration import ConformalCalibrator, compute_ece
from legal_sheaf.config import ARTIFACTS_DIR
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.losses import MultiTaskSheafLoss
from legal_sheaf.onnx_runtime import export_sheaf_to_onnx
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import DocumentInput, LegalRegime
from legal_sheaf.sheaf_model import LegalSheafNet16x41

logger = logging.getLogger("legal_sheaf.continual_learner")


# ---------------------------------------------------------------------------
# 1. PSI Feature Drift Detector
# ---------------------------------------------------------------------------

def calculate_psi(
    baseline_matrix: np.ndarray,
    current_matrix: np.ndarray,
    num_bins: int = 10,
    epsilon: float = 1e-4,
) -> Dict[str, Any]:
    """
    Computes Population Stability Index (PSI) for each of the 16 slots.
    baseline_matrix: [N_base, 16] or [N_base, 16, F]
    current_matrix:  [N_curr, 16] or [N_curr, 16, F]
    """
    # If 3D, take mean across feature dimension to get per-slot intensity [N, 16]
    if baseline_matrix.ndim == 3:
        b_mat = np.mean(baseline_matrix, axis=-1)
        c_mat = np.mean(current_matrix, axis=-1)
    else:
        b_mat = baseline_matrix
        c_mat = current_matrix

    num_slots = b_mat.shape[1]
    slot_psi: Dict[int, float] = {}
    drifted_slots: List[Dict[str, Any]] = []

    for s in range(num_slots):
        b_vals = b_mat[:, s]
        c_vals = c_mat[:, s]

        # Determine bin edges from baseline quantiles
        quantiles = np.linspace(0, 100, num_bins + 1)
        bin_edges = np.percentile(b_vals, quantiles)
        bin_edges = np.unique(bin_edges)  # Remove duplicates if constant

        if len(bin_edges) < 2:
            # Baseline is constant for this slot
            diff = np.abs(np.mean(c_vals) - np.mean(b_vals))
            psi = float(diff * 10.0)
        else:
            # Standard continuous PSI: outer bins must cover (-inf, +inf)
            # so that out-of-range shifted values are captured in edge bins
            bin_edges[0] = -np.inf
            bin_edges[-1] = np.inf

            b_counts, _ = np.histogram(b_vals, bins=bin_edges)
            c_counts, _ = np.histogram(c_vals, bins=bin_edges)

            # Convert to proportions with smoothing
            p = (b_counts + epsilon) / (np.sum(b_counts) + epsilon * len(b_counts))
            q = (c_counts + epsilon) / (np.sum(c_counts) + epsilon * len(c_counts))

            psi = float(np.sum((q - p) * np.log(q / p)))

        slot_psi[s] = round(psi, 4)
        if psi > 0.25:
            drifted_slots.append({
                "slot_id": s,
                "psi": round(psi, 4),
                "severity": "CRITICAL" if psi > 0.50 else "MODERATE",
            })

    max_psi = max(slot_psi.values()) if slot_psi else 0.0
    mean_psi = float(np.mean(list(slot_psi.values()))) if slot_psi else 0.0
    has_drift = max_psi > 0.25

    alert = None
    if has_drift:
        worst_slot = max(slot_psi, key=slot_psi.get)
        alert = (
            f"LAYOUT_DRIFT_DETECTED: Feature drift detected on slot {worst_slot} "
            f"(PSI={slot_psi[worst_slot]} > 0.25)"
        )

    return {
        "has_drift": has_drift,
        "max_psi": round(max_psi, 4),
        "mean_psi": round(mean_psi, 4),
        "slot_psi": slot_psi,
        "drifted_slots": drifted_slots,
        "alert": alert,
    }


# ---------------------------------------------------------------------------
# 2. Zero-Retrain Law Ingestion
# ---------------------------------------------------------------------------

def ingest_kb_inbox(
    bundle: AdRadarBrainBundle,
    inbox_dir: Union[str, Path],
) -> Tuple[AdRadarBrainBundle, int]:
    """
    Ingests new statutes, amendments, FAS letters, and court cases from inbox directory
    directly into the bundle's precedent index and vector matrix WITHOUT retraining neural weights.
    Returns: (updated_bundle, count_new_ingested)
    """
    inbox_path = Path(inbox_dir)
    if not inbox_path.exists():
        return bundle, 0

    new_items: List[Dict[str, Any]] = []
    for file_path in inbox_path.glob("*"):
        if file_path.suffix.lower() in (".jsonl", ".json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    if file_path.suffix.lower() == ".jsonl":
                        for line in f:
                            if line.strip():
                                new_items.append(json.loads(line))
                    else:
                        data = json.load(f)
                        if isinstance(data, list):
                            new_items.extend(data)
                        elif isinstance(data, dict):
                            new_items.append(data)
            except Exception as e:
                logger.error(f"Error parsing inbox file {file_path}: {e}")

    if not new_items:
        return bundle, 0

    # Build new embeddings and format records
    existing_precedents = list(bundle.precedents)
    existing_embeddings = bundle.precedent_embeddings  # [N, dim]
    new_embeddings_list = []
    dates_found = []

    dim = existing_embeddings.shape[1] if existing_embeddings.ndim > 1 else 256
    prec_idx = bundle._precedent_index or LegalPrecedentIndex(use_fast_ngram_fallback=True)

    for item in new_items:
        # Standardize fields
        text = f"{item.get('title', '')} {item.get('text', item.get('summary', ''))}"
        item["_search_text"] = text
        item["_type"] = item.get("type", "statute")
        item["_is_guilty"] = not item.get("is_exemption", False)
        item["_is_safe"] = item.get("is_exemption", False)

        vec = prec_idx._compute_ngram_hash_vector(text, dim=dim)
        new_embeddings_list.append(vec)
        existing_precedents.append(item)

        d = item.get("effective_from") or item.get("enacted_date") or item.get("date")
        if d:
            dates_found.append(str(d))

    new_embeddings = np.stack(new_embeddings_list, axis=0)
    combined_embeddings = np.vstack([existing_embeddings, new_embeddings])

    # Update manifest
    updated_manifest = dict(bundle.manifest)
    updated_manifest["num_precedents"] = len(existing_precedents)
    if dates_found:
        latest_date = max(dates_found)
        if latest_date > updated_manifest.get("statute_cutoff_date", ""):
            updated_manifest["statute_cutoff_date"] = latest_date

    updated_manifest["last_kb_ingestion"] = datetime.now().isoformat()

    updated_bundle = AdRadarBrainBundle(
        onnx_bytes=bundle.onnx_bytes,
        calibration=bundle.calibration,
        precedents=existing_precedents,
        precedent_embeddings=combined_embeddings,
        manifest=updated_manifest,
    )
    logger.info(f"Ingested {len(new_items)} new legal items into bundle without code change.")
    return updated_bundle, len(new_items)


# ---------------------------------------------------------------------------
# 3. Champion / Challenger Promotion Gate
# ---------------------------------------------------------------------------

@dataclass
class PromotionDecision:
    promoted: bool
    reason: str
    golden_precision: float
    golden_recall: float
    golden_ece: float
    new_recall: float
    champion_new_recall: float
    metrics: Dict[str, float] = field(default_factory=dict)


class ContinualLearner:
    """
    Continual Learning Engine with Experience Replay, L2-SP Regularization,
    and strict Champion/Challenger Quality Gate.
    """

    def __init__(
        self,
        champion_bundle: AdRadarBrainBundle,
        lambda_sp: float = 0.05,
        target_precision: float = 0.96,
        max_ece: float = 0.05,
    ):
        self.champion_bundle = champion_bundle
        self.lambda_sp = lambda_sp
        self.target_precision = target_precision
        self.max_ece = max_ece
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def _init_torch_model_from_checkpoint(
        self,
        checkpoint_path: Optional[Union[str, Path]] = None,
    ) -> LegalSheafNet16x41:
        """Loads PyTorch model for training."""
        model = LegalSheafNet16x41().to(self.device)
        ckpt_path = Path(checkpoint_path) if checkpoint_path else ARTIFACTS_DIR / "legal_sheaf_model.pt"
        if ckpt_path.exists():
            ckpt = torch.load(ckpt_path, map_location=self.device)
            model.load_state_dict(ckpt["model_state_dict"])
        if hasattr(self, "champion_bundle") and hasattr(self.champion_bundle, "calibration"):
            model.temperature = float(self.champion_bundle.calibration.get("temperature", 1.0) or 1.0)
        else:
            model.temperature = 1.0
        return model

    def evaluate_model_on_dataset(
        self,
        model: nn.Module,
        dataset: List[Tuple[torch.Tensor, int]],  # [(tensor[16, 36], label)]
        threshold: float,
    ) -> Dict[str, float]:
        """Evaluates precision, recall, and ECE for binary violation detection."""
        model.eval()
        probs_list = []
        labels_list = []
        temp = getattr(model, "temperature", 1.0) or 1.0

        with torch.no_grad():
            for x, y in dataset:
                if x.ndim == 2:
                    x = x.unsqueeze(0)
                x = x.to(self.device)
                out = model(x)
                viol_logit = out["violation_logit"] / temp
                prob = torch.sigmoid(viol_logit).item()
                probs_list.append(prob)
                labels_list.append(y)

        probs = np.array(probs_list)
        labels = np.array(labels_list)
        preds = (probs >= threshold).astype(int)

        tp = np.sum((preds == 1) & (labels == 1))
        fp = np.sum((preds == 1) & (labels == 0))
        fn = np.sum((preds == 0) & (labels == 1))
        tn = np.sum((preds == 0) & (labels == 0))

        precision = float(tp / (tp + fp)) if (tp + fp) > 0 else 1.0
        recall = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        ece = compute_ece(probs, labels, num_bins=10)

        return {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "ece": round(ece, 4),
            "tp": int(tp),
            "fp": int(fp),
            "fn": int(fn),
            "tn": int(tn),
        }

    def train_challenger(
        self,
        golden_data: List[Tuple[torch.Tensor, int]],     # 75% invariant archetypes
        new_feedback: List[Tuple[torch.Tensor, int]],    # 25% verified feedback
        epochs: int = 8,
        lr: float = 1e-4,
    ) -> Tuple[LegalSheafNet16x41, float]:
        """
        Trains challenger using Experience Replay and L2-SP Regularization:
        L = L_bce + (lambda_sp / 2) * ||theta - theta_champion||^2 + lambda_orth * L_orth
        """
        challenger = self._init_torch_model_from_checkpoint()
        champion_params = {n: p.clone().detach() for n, p in challenger.named_parameters()}

        # Experience Replay Mixing (75% golden, 25% new)
        replay_samples = []
        # Oversample new feedback to reach target 25% ratio if small
        n_golden = len(golden_data)
        target_new = max(len(new_feedback), int(n_golden * 0.25))
        
        replay_samples.extend(golden_data)
        if new_feedback:
            factor = math.ceil(target_new / len(new_feedback))
            extended_new = (new_feedback * factor)[:target_new]
            replay_samples.extend(extended_new)

        optimizer = torch.optim.AdamW(challenger.parameters(), lr=lr, weight_decay=1e-5)
        criterion = nn.BCEWithLogitsLoss()

        challenger.train()
        for epoch in range(epochs):
            np.random.shuffle(replay_samples)
            total_loss = 0.0

            for x, y in replay_samples:
                if x.ndim == 2:
                    x = x.unsqueeze(0)
                x = x.to(self.device)
                target = torch.tensor([[float(y)]], device=self.device)

                optimizer.zero_grad()
                out = challenger(x)
                viol_logit = out["violation_logit"]

                # 1. Task loss
                loss_task = criterion(viol_logit, target)

                # 2. L2-SP Regularization towards Champion parameters
                l2_sp = torch.tensor(0.0, device=self.device)
                for n, p in challenger.named_parameters():
                    if n in champion_params:
                        l2_sp = l2_sp + torch.sum((p - champion_params[n]) ** 2)

                # 3. Sheaf Stiefel Orthogonality
                l_orth = challenger.compute_stiefel_orthogonality_loss()

                loss = loss_task + (self.lambda_sp * 0.5 * l2_sp) + (0.01 * l_orth)
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        # Recalibrate conformal threshold on golden data
        challenger.eval()
        logits_list = []
        labels_list = []
        with torch.no_grad():
            for x, y in golden_data:
                if x.ndim == 2:
                    x = x.unsqueeze(0)
                out = challenger(x.to(self.device))
                logits_list.append(float(out["violation_logit"].view(-1)[0].item()))
                labels_list.append(float(y))

        calibrator = ConformalCalibrator(target_precision=self.target_precision)
        calibrator.calibrate(np.array(logits_list), np.array(labels_list))
        threshold = float(calibrator.decision_threshold)
        challenger.temperature = float(getattr(calibrator, "temperature_val", 1.0) or 1.0)

        return challenger, threshold

    def run_champion_challenger_gate(
        self,
        golden_dataset: List[Tuple[torch.Tensor, int]],
        new_feedback: List[Tuple[torch.Tensor, int]],
        challenger_model: Optional[LegalSheafNet16x41] = None,
        challenger_threshold: Optional[float] = None,
        epochs: int = 8,
    ) -> Tuple[PromotionDecision, Optional[AdRadarBrainBundle]]:
        """
        Executes full continual learning loop and gate:
        1. Trains challenger if not provided.
        2. Evaluates Champion on new data.
        3. Evaluates Challenger on Golden set (invariants) and New data.
        4. Verifies Gate: Golden Precision >= 0.96, Golden ECE <= 0.05, New Recall improved.
        5. Returns PromotionDecision and new Champion bundle if promoted.
        """
        # Baseline Champion performance
        champ_calib_thresh = self.champion_bundle.decision_threshold
        champ_model = self._init_torch_model_from_checkpoint()
        champ_new_eval = self.evaluate_model_on_dataset(champ_model, new_feedback, champ_calib_thresh)
        champ_new_recall = champ_new_eval["recall"]

        # Train challenger if not passed
        if challenger_model is None or challenger_threshold is None:
            challenger_model, challenger_threshold = self.train_challenger(
                golden_data=golden_dataset,
                new_feedback=new_feedback,
                epochs=epochs,
            )

        # Evaluate Challenger
        golden_eval = self.evaluate_model_on_dataset(challenger_model, golden_dataset, challenger_threshold)
        challenger_new_eval = self.evaluate_model_on_dataset(challenger_model, new_feedback, challenger_threshold)

        gold_prec = golden_eval["precision"]
        gold_rec = golden_eval["recall"]
        gold_ece = golden_eval["ece"]
        chal_new_recall = challenger_new_eval["recall"]

        # Gate Checks
        gate_1_precision = gold_prec >= self.target_precision
        gate_2_ece = gold_ece <= self.max_ece
        gate_3_recall = chal_new_recall >= champ_new_recall

        if gate_1_precision and gate_2_ece and gate_3_recall:
            # PROMOTION GRANTED
            reason = (
                f"Promotion APPROVED: Golden Precision {gold_prec} >= {self.target_precision}, "
                f"ECE {gold_ece} <= {self.max_ece}, and New Recall improved from {champ_new_recall} to {chal_new_recall}."
            )
            decision = PromotionDecision(
                promoted=True,
                reason=reason,
                golden_precision=gold_prec,
                golden_recall=gold_rec,
                golden_ece=gold_ece,
                new_recall=chal_new_recall,
                champion_new_recall=champ_new_recall,
                metrics={**golden_eval, "new_recall": chal_new_recall},
            )

            # Export new ONNX model and build promoted bundle
            tmp_onnx = ARTIFACTS_DIR / "challenger_promoted.onnx"
            export_sheaf_to_onnx(challenger_model, tmp_onnx)
            with open(tmp_onnx, "rb") as f:
                new_onnx_bytes = f.read()
            tmp_onnx.unlink(missing_ok=True)

            new_calib = dict(self.champion_bundle.calibration)
            new_calib["decision_threshold"] = challenger_threshold

            new_manifest = dict(self.champion_bundle.manifest)
            ver_parts = new_manifest.get("bundle_version", "1.0.0").split(".")
            ver_parts[-1] = str(int(ver_parts[-1]) + 1)
            new_manifest["bundle_version"] = ".".join(ver_parts)
            new_manifest["certified_metrics"] = {
                "precision": gold_prec,
                "recall": gold_rec,
                "ece": gold_ece,
                "new_pattern_recall": chal_new_recall,
            }
            new_manifest["promoted_at"] = datetime.now().isoformat()

            promoted_bundle = AdRadarBrainBundle(
                onnx_bytes=new_onnx_bytes,
                calibration=new_calib,
                precedents=self.champion_bundle.precedents,
                precedent_embeddings=self.champion_bundle.precedent_embeddings,
                manifest=new_manifest,
            )
            logger.info(f"Champion promoted to version {new_manifest['bundle_version']}: {reason}")
            return decision, promoted_bundle

        else:
            # PROMOTION REJECTED
            failures = []
            if not gate_1_precision:
                failures.append(f"Golden Precision {gold_prec} < {self.target_precision}")
            if not gate_2_ece:
                failures.append(f"Golden ECE {gold_ece} > {self.max_ece}")
            if not gate_3_recall:
                failures.append(f"New Recall {chal_new_recall} < Champion Recall {champ_new_recall}")

            reason = f"Promotion REJECTED: {', '.join(failures)}. Rollback to Champion."
            decision = PromotionDecision(
                promoted=False,
                reason=reason,
                golden_precision=gold_prec,
                golden_recall=gold_rec,
                golden_ece=gold_ece,
                new_recall=chal_new_recall,
                champion_new_recall=champ_new_recall,
                metrics={**golden_eval, "new_recall": chal_new_recall},
            )
            logger.warning(reason)
            return decision, None
