"""
Multi-Task Loss for LegalSheafNet-16x41.
Combines:
- Focal Binary Cross Entropy for violation classification
- Cross Entropy for 7-class Legal Regime
- Auxiliary BCE for 16 slot verification probabilities
- Contrastive Sheaf Energy Margin Loss
- Stiefel Manifold Orthogonality Regularization
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class MultiTaskSheafLoss(nn.Module):
    """
    Complete multi-task loss function for training LegalSheafNet-16x41.
    """

    def __init__(
        self,
        focal_gamma: float = 2.0,
        focal_alpha: float = 0.65,
        lambda_regime: float = 1.0,
        lambda_slot_aux: float = 0.5,
        lambda_energy_contrastive: float = 0.3,
        lambda_stiefel_orth: float = 0.1,
        energy_margin: float = 0.4,
    ):
        super().__init__()
        self.focal_gamma = focal_gamma
        self.focal_alpha = focal_alpha
        self.lambda_regime = lambda_regime
        self.lambda_slot_aux = lambda_slot_aux
        self.lambda_energy_contrastive = lambda_energy_contrastive
        self.lambda_stiefel_orth = lambda_stiefel_orth
        self.energy_margin = energy_margin

        self.regime_ce = nn.CrossEntropyLoss()

    def forward(
        self,
        preds: dict[str, torch.Tensor],
        targets: dict[str, torch.Tensor],
        stiefel_orth_loss: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        """
        preds: model forward outputs
        targets:
            - 'is_violation': [Batch] (0 or 1 float)
            - 'regime': [Batch] (int 0..6)
            - 'slot_labels': [Batch, 16] (binary float 0 or 1 for slot ground-truth)
        stiefel_orth_loss: computed from model.compute_stiefel_orthogonality_loss()
        """
        # 1. Focal BCE on violation_logit
        v_logits = preds["violation_logit"].squeeze(-1)  # [B]
        v_targets = targets["is_violation"].float()      # [B]
        
        bce = F.binary_cross_entropy_with_logits(v_logits, v_targets, reduction="none")
        p = torch.sigmoid(v_logits)
        p_t = p * v_targets + (1.0 - p) * (1.0 - v_targets)
        alpha_t = self.focal_alpha * v_targets + (1.0 - self.focal_alpha) * (1.0 - v_targets)
        focal_factor = alpha_t * ((1.0 - p_t) ** self.focal_gamma)
        loss_violation = torch.mean(focal_factor * bce)

        # 2. Regime 7-class Cross Entropy
        regime_logits = preds["regime_logits"]  # [B, 7]
        regime_targets = targets["regime"].long()  # [B]
        loss_regime = self.regime_ce(regime_logits, regime_targets)

        # 3. Auxiliary BCE on 16 Slot verified probabilities
        slot_probs = preds["slot_probabilities"]  # [B, 16]
        slot_targets = targets["slot_labels"].float()  # [B, 16]
        # slot_probs is already in [0, 1]
        slot_probs_clamped = torch.clamp(slot_probs, 1e-6, 1.0 - 1e-6)
        loss_slot_aux = F.binary_cross_entropy(slot_probs_clamped, slot_targets)

        # 4. Contrastive Sheaf Energy Margin Loss
        # Violation cases should exhibit high edge conflict energy;
        # Lawful clean documents should have low, quenched sheaf energy.
        total_energy = preds["total_sheaf_energy"].squeeze(-1)  # [B]
        energy_violation_penalty = v_targets * F.relu(self.energy_margin - total_energy)
        energy_clean_penalty = 2.5 * (1.0 - v_targets) * total_energy
        loss_energy_contrastive = torch.mean(energy_violation_penalty + energy_clean_penalty)

        # 5. Stiefel Orthogonality Loss
        loss_stiefel = stiefel_orth_loss

        # Total combined loss
        loss_total = (
            loss_violation
            + self.lambda_regime * loss_regime
            + self.lambda_slot_aux * loss_slot_aux
            + self.lambda_energy_contrastive * loss_energy_contrastive
            + self.lambda_stiefel_orth * loss_stiefel
        )

        return {
            "loss_total": loss_total,
            "loss_violation": loss_violation,
            "loss_regime": loss_regime,
            "loss_slot_aux": loss_slot_aux,
            "loss_energy_contrastive": loss_energy_contrastive,
            "loss_stiefel_orth": loss_stiefel,
        }
