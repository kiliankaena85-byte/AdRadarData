"""
Compact Interpretable Link-Centric Classifier: LinkCentricAdClassifierV3.
Replaces the over-parameterized 16x41 cellular sheaf with:
1. Built-in ONNX-safe feature standardization (feat_mean, feat_std buffers)
2. Interpretable linear skip path producing per-feature attributions
3. Compact regularized residual MLP + 7-regime probabilistic consistency
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from legal_sheaf.link_features import FEATURE_DIM
from legal_sheaf.schema import LegalRegime

NUM_FEATURES = FEATURE_DIM
NUM_REGIMES = len(LegalRegime)


class LinkCentricAdClassifierV3(nn.Module):
    """
    Compact ~5.7K-parameter residual classifier over 48 Link-Centric features.
    Designed to generalize from real-world labeled VC.ru articles without overfitting.
    """

    def __init__(
        self,
        in_dim: int = NUM_FEATURES,
        hidden_dim: int = 64,
        bottleneck_dim: int = 32,
        num_regimes: int = NUM_REGIMES,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.hidden_dim = hidden_dim
        self.bottleneck_dim = bottleneck_dim
        self.num_regimes = num_regimes

        self.register_buffer("feat_mean", torch.zeros(in_dim, dtype=torch.float32))
        self.register_buffer("feat_std", torch.ones(in_dim, dtype=torch.float32))

        self.linear_viol = nn.Linear(in_dim, 1)

        self.mlp = nn.Sequential(
            nn.Linear(in_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Dropout(0.25),
            nn.Linear(hidden_dim, bottleneck_dim),
            nn.GELU(),
            nn.Dropout(0.15),
        )
        self.residual_viol_head = nn.Linear(bottleneck_dim, 1)
        self.regime_head = nn.Linear(bottleneck_dim, num_regimes)

    def set_normalization(self, mean: np.ndarray | torch.Tensor, std: np.ndarray | torch.Tensor) -> None:
        """Stores training feature mean and std inside model buffers for ONNX parity."""
        mean_t = torch.as_tensor(mean, dtype=torch.float32, device=self.feat_mean.device).reshape(self.in_dim)
        std_t = torch.as_tensor(std, dtype=torch.float32, device=self.feat_std.device).reshape(self.in_dim)
        std_t = torch.clamp(std_t, min=1e-2)
        self.feat_mean.copy_(mean_t)
        self.feat_std.copy_(std_t)

    def forward(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        x: [Batch, 48] float32 feature tensor.
        Returns dict with violation_logit [B, 1], regime_logits [B, 7], feature_attributions [B, 48].
        """
        x_norm = (x - self.feat_mean) / torch.clamp(self.feat_std, min=1e-4)
        feature_attributions = x_norm * self.linear_viol.weight

        linear_logit = self.linear_viol(x_norm)
        h = self.mlp(x_norm)
        raw_viol = linear_logit + self.residual_viol_head(h)
        regime_logits = self.regime_head(h)

        # Enforce probabilistic consistency between 7-regime head and binary violation head:
        # Violating regimes = {2, 3, 4, 5}; Safe regimes = {0, 1, 6}
        reg_probs = torch.softmax(regime_logits, dim=-1)
        p_viol_sum = reg_probs[:, 2:6].sum(dim=-1, keepdim=True)
        p_safe_sum = reg_probs[:, 0:2].sum(dim=-1, keepdim=True) + reg_probs[:, 6:7]
        regime_log_odds = torch.log(torch.clamp(p_viol_sum, min=1e-6)) - torch.log(
            torch.clamp(p_safe_sum, min=1e-6)
        )
        violation_logit = 0.55 * raw_viol + 0.45 * regime_log_odds

        return {
            "violation_logit": violation_logit,
            "regime_logits": regime_logits,
            "feature_attributions": feature_attributions,
        }


class LinkClassifierLoss(nn.Module):
    """
    Sample-weighted multi-task loss combining Focal BCE (binary violation)
    and Cross-Entropy (7-class LegalRegime).
    """

    def __init__(
        self,
        focal_gamma: float = 1.5,
        focal_alpha: float = 0.60,
        lambda_regime: float = 0.5,
        label_smoothing: float = 0.0,
    ):
        super().__init__()
        self.focal_gamma = focal_gamma
        self.focal_alpha = focal_alpha
        self.lambda_regime = lambda_regime
        self.label_smoothing = float(label_smoothing)

    def forward(
        self,
        preds: Dict[str, torch.Tensor],
        targets: Dict[str, torch.Tensor],
        sample_weights: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        v_logits = preds["violation_logit"].squeeze(-1)
        v_targets = targets["is_violation"].float()
        eps = max(0.0, min(0.15, self.label_smoothing))
        v_targets_smooth = v_targets * (1.0 - 2.0 * eps) + eps

        bce = F.binary_cross_entropy_with_logits(v_logits, v_targets_smooth, reduction="none")
        p = torch.sigmoid(v_logits)
        p_t = p * v_targets + (1.0 - p) * (1.0 - v_targets)
        alpha_t = self.focal_alpha * v_targets + (1.0 - self.focal_alpha) * (1.0 - v_targets)
        focal_loss_vec = alpha_t * ((1.0 - p_t) ** self.focal_gamma) * bce

        regime_logits = preds["regime_logits"]
        regime_targets = targets["regime"].long()
        ce_vec = F.cross_entropy(regime_logits, regime_targets, reduction="none", label_smoothing=eps)

        if sample_weights is not None:
            w = torch.clamp(sample_weights.float(), min=1e-4)
            w_sum = torch.clamp(w.sum(), min=1e-6)
            loss_violation = torch.sum(focal_loss_vec * w) / w_sum
            loss_regime = torch.sum(ce_vec * w) / w_sum
        else:
            loss_violation = torch.mean(focal_loss_vec)
            loss_regime = torch.mean(ce_vec)

        loss_total = loss_violation + self.lambda_regime * loss_regime
        return {
            "loss_total": loss_total,
            "loss_violation": loss_violation,
            "loss_regime": loss_regime,
        }
