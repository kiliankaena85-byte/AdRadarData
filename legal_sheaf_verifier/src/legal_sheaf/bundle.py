"""
Atomic Brain Bundle Packaging for AdRadar v3.0 (LinkCentricAdClassifierV3).
Encapsulates compact ONNX classifier graph, feature schema, conformal calibration,
precalculated legal KB reference vectors, and SHA-256 signed manifest into a single .bundle archive.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import onnxruntime as ort
import torch

from legal_sheaf.config import ARTIFACTS_DIR
from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.link_features import (
    FEATURE_DIM,
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    LinkCentricFeatureExtractor,
    resolve_is_own_website,
)
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import DocumentInput, VerificationReport

logger = logging.getLogger("legal_sheaf.bundle")


def compute_sha256_bytes(data: bytes) -> str:
    """Computes SHA-256 hex digest for binary data."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path: Union[str, Path]) -> str:
    """Computes SHA-256 hex digest for a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class IntegrityError(Exception):
    """Raised when bundle SHA-256 signature or internal checksum verification fails."""
    pass


class AdRadarBrainBundle:
    """
    Single-file atomic deployment bundle for LinkCentricAdClassifierV3.
    Provides sub-millisecond ONNX inference and decoupled legal KB reference lookup.
    """

    def __init__(
        self,
        onnx_bytes: bytes,
        calibration: Dict[str, Any],
        precedents: List[Dict[str, Any]],
        precedent_embeddings: np.ndarray,
        manifest: Dict[str, Any],
        feature_schema: Optional[Dict[str, Any]] = None,
    ):
        self.onnx_bytes = onnx_bytes
        self.calibration = calibration
        self.precedents = precedents
        self.precedent_embeddings = precedent_embeddings
        self.manifest = manifest
        self.feature_schema = feature_schema or {
            "version": FEATURE_SCHEMA_VERSION,
            "feature_dim": FEATURE_DIM,
            "feature_names": list(FEATURE_NAMES),
        }

        self._onnx_session: Optional[ort.InferenceSession] = None
        self._precedent_index: Optional[LegalPrecedentIndex] = None
        self._feature_builder: Optional[LinkCentricFeatureExtractor] = None
        self._explainer: Optional[LegalSheafExplainer] = None

        self._init_runtime()

    def _init_runtime(self) -> None:
        """Initializes ONNX inference session, feature extractor, and decoupled reference index."""
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._onnx_session = ort.InferenceSession(self.onnx_bytes, opts, providers=["CPUExecutionProvider"])

        emb_dim = int(self.precedent_embeddings.shape[1]) if self.precedent_embeddings.ndim == 2 else 256
        prefer_ngram = bool(self.manifest.get("use_fast_ngram_fallback", emb_dim == 256))
        if emb_dim == 256:
            prefer_ngram = True

        target_device = "cuda" if torch.cuda.is_available() else "cpu"
        self._feature_builder = LinkCentricFeatureExtractor(
            device=target_device,
            use_fast_ngram_fallback=prefer_ngram,
        )

        # Precedent index is strictly decoupled from ML classifier inputs (legal reference / regional check only)
        self._precedent_index = LegalPrecedentIndex(
            device=target_device,
            use_fast_ngram_fallback=self._feature_builder.use_fast_ngram_fallback,
            skip_index_build=True,
        )
        self._precedent_index.all_precedents = list(self.precedents)
        if not self._precedent_index.use_fast_ngram_fallback and self._precedent_index.encoder is not None:
            self._precedent_index.embeddings = torch.from_numpy(self.precedent_embeddings).float()
        elif emb_dim != 256 and self._precedent_index.use_fast_ngram_fallback:
            self._precedent_index._build_vector_index()
        else:
            self._precedent_index.embeddings = torch.from_numpy(self.precedent_embeddings).float()

        self._explainer = LegalSheafExplainer(precedent_index=self._precedent_index)

    @property
    def version(self) -> str:
        return self.manifest.get("bundle_version", "3.0.0")

    @property
    def statute_cutoff_date(self) -> str:
        return self.manifest.get("statute_cutoff_date", "2026-06-18")

    @property
    def decision_threshold(self) -> float:
        return float(self.calibration.get("decision_threshold", 0.50))

    @property
    def abstain_lower(self) -> float:
        return float(self.calibration.get("abstain_lower", 0.35))

    @property
    def abstain_upper(self) -> float:
        return float(self.calibration.get("abstain_upper", 0.62))

    @property
    def certified_metrics(self) -> Dict[str, float]:
        return self.manifest.get("certified_metrics", {})

    def predict_features(self, x: np.ndarray) -> Dict[str, np.ndarray]:
        """Runs ONNX Runtime inference on input feature matrix [B, 48] or [48]."""
        arr = np.asarray(x, dtype=np.float32)
        if arr.ndim == 1:
            arr = np.expand_dims(arr, 0)
        input_name = self._onnx_session.get_inputs()[0].name
        outputs = self._onnx_session.run(None, {input_name: arr})
        output_names = [o.name for o in self._onnx_session.get_outputs()]
        return dict(zip(output_names, outputs))

    # Backward-compatible alias
    predict_tensor = predict_features

    def verify_document(self, doc: DocumentInput) -> VerificationReport:
        """
        End-to-end audit:
        - OWN_WEBSITE short-circuits directly to deterministic statutory rules (< 1 ms, zero GPU/ONNX waste)
        - EXTERNAL_PUBLICATION runs LinkCentricFeatureExtractor -> ONNX LinkCentricAdClassifierV3 -> Explainer
        """
        t0 = time.perf_counter()
        if resolve_is_own_website(doc):
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return self._explainer.explain(
                doc=doc,
                model_output=None,
                conformal_threshold=self.decision_threshold,
                latency_ms=latency_ms,
                temperature=float(self.calibration.get("temperature_val", 1.0)),
                abstain_lower=self.abstain_lower,
                abstain_upper=self.abstain_upper,
            )

        diag = self._feature_builder.extract_with_diagnostics(doc)
        feat_vec = diag["features"]  # [48]
        onnx_out = self.predict_features(feat_vec)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        return self._explainer.explain(
            doc=doc,
            model_output=onnx_out,
            conformal_threshold=self.decision_threshold,
            latency_ms=latency_ms,
            temperature=float(self.calibration.get("temperature_val", 1.0)),
            abstain_lower=self.abstain_lower,
            abstain_upper=self.abstain_upper,
            feature_values=feat_vec,
            link_windows=diag["anchor_windows"],
            tier0_flags=diag["tier0_flags"],
        )

    def verify_integrity(self) -> Dict[str, Any]:
        """Verifies internal SHA-256 checksums of all bundled components against manifest."""
        components_hash = {
            "model.onnx": compute_sha256_bytes(self.onnx_bytes),
            "conformal_calibration.json": compute_sha256_bytes(json.dumps(self.calibration, sort_keys=True).encode()),
            "feature_schema.json": compute_sha256_bytes(json.dumps(self.feature_schema, sort_keys=True).encode()),
            "precedents.json": compute_sha256_bytes(json.dumps(self.precedents, sort_keys=True).encode()),
            "precedent_embeddings.npy": compute_sha256_bytes(self.precedent_embeddings.tobytes()),
        }

        manifest_hashes = self.manifest.get("sha256_components", {})
        mismatches = []
        for name, current_hash in components_hash.items():
            expected_hash = manifest_hashes.get(name)
            if expected_hash and current_hash != expected_hash:
                mismatches.append({"component": name, "expected": expected_hash, "actual": current_hash})

        return {
            "is_valid": len(mismatches) == 0,
            "bundle_version": self.version,
            "statute_cutoff_date": self.statute_cutoff_date,
            "mismatches": mismatches,
            "sha256_components": components_hash,
        }

    def save(self, path: Union[str, Path]) -> Path:
        """Saves bundle into a single atomic zip archive (.bundle)."""
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        components_hash = {
            "model.onnx": compute_sha256_bytes(self.onnx_bytes),
            "conformal_calibration.json": compute_sha256_bytes(json.dumps(self.calibration, sort_keys=True).encode()),
            "feature_schema.json": compute_sha256_bytes(json.dumps(self.feature_schema, sort_keys=True).encode()),
            "precedents.json": compute_sha256_bytes(json.dumps(self.precedents, sort_keys=True).encode()),
            "precedent_embeddings.npy": compute_sha256_bytes(self.precedent_embeddings.tobytes()),
        }
        self.manifest["sha256_components"] = components_hash
        self.manifest["packaged_at"] = datetime.now().isoformat()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bundle.tmp") as tmp:
            tmp_path = Path(tmp.name)

        try:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("model.onnx", self.onnx_bytes)
                z.writestr("conformal_calibration.json", json.dumps(self.calibration, ensure_ascii=False, indent=2))
                z.writestr("feature_schema.json", json.dumps(self.feature_schema, ensure_ascii=False, indent=2))
                z.writestr("precedents.json", json.dumps(self.precedents, ensure_ascii=False, indent=2))

                emb_buf = io.BytesIO()
                np.save(emb_buf, self.precedent_embeddings)
                z.writestr("precedent_embeddings.npy", emb_buf.getvalue())

                z.writestr("manifest.json", json.dumps(self.manifest, ensure_ascii=False, indent=2))

            if out_path.exists():
                out_path.unlink()
            tmp_path.rename(out_path)
            logger.info(f"Successfully packaged AdRadarBrainBundle to: {out_path} ({out_path.stat().st_size} bytes)")
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return out_path

    @classmethod
    def load(cls, path: Union[str, Path], verify_hash: bool = True) -> AdRadarBrainBundle:
        """Loads and initializes an AdRadarBrainBundle from a .bundle archive."""
        bundle_path = Path(path)
        if not bundle_path.exists():
            raise FileNotFoundError(f"Bundle file not found: {bundle_path}")

        with zipfile.ZipFile(bundle_path, "r") as z:
            names = set(z.namelist())
            onnx_bytes = z.read("model.onnx")
            calibration = json.loads(z.read("conformal_calibration.json").decode("utf-8"))
            precedents = json.loads(z.read("precedents.json").decode("utf-8"))
            feature_schema = (
                json.loads(z.read("feature_schema.json").decode("utf-8"))
                if "feature_schema.json" in names
                else None
            )
            emb_buf = io.BytesIO(z.read("precedent_embeddings.npy"))
            precedent_embeddings = np.load(emb_buf)
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))

        bundle = cls(
            onnx_bytes=onnx_bytes,
            calibration=calibration,
            precedents=precedents,
            precedent_embeddings=precedent_embeddings,
            manifest=manifest,
            feature_schema=feature_schema,
        )

        if verify_hash:
            check = bundle.verify_integrity()
            if not check["is_valid"]:
                raise IntegrityError(f"Bundle integrity check failed for {bundle_path}: {check['mismatches']}")

        return bundle

    @classmethod
    def create_from_artifacts(
        cls,
        artifacts_dir: Optional[Union[str, Path]] = None,
        kb_seed_dir: Optional[Union[str, Path]] = None,
        bundle_version: str = "3.0.0",
        certified_metrics: Optional[Dict[str, Any]] = None,
        use_fast_ngram_fallback: bool = False,
        encoder_device: Optional[str] = None,
        manifest_extra: Optional[Dict[str, Any]] = None,
    ) -> AdRadarBrainBundle:
        """Builds a fresh v3 bundle from local project artifacts and legal KB reference seed."""
        art_path = Path(artifacts_dir) if artifacts_dir else ARTIFACTS_DIR
        onnx_path = art_path / "link_classifier_v3.onnx"
        cal_path = art_path / "conformal_calibration.json"
        ckpt_path = art_path / "link_classifier_v3.pt"

        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model missing at {onnx_path}")

        onnx_bytes = onnx_path.read_bytes()
        if cal_path.exists():
            calibration = json.loads(cal_path.read_text(encoding="utf-8"))
        else:
            calibration = {"decision_threshold": 0.50}

        target_device = encoder_device or ("cuda" if torch.cuda.is_available() else "cpu")
        index = LegalPrecedentIndex(
            seed_dir=str(kb_seed_dir) if kb_seed_dir else None,
            device=target_device,
            use_fast_ngram_fallback=use_fast_ngram_fallback,
        )
        precedents = index.all_precedents
        precedent_embeddings = index.embeddings.cpu().numpy()

        dates = [str(p.get("effective_from") or p.get("enacted_date")) for p in precedents if p.get("effective_from") or p.get("enacted_date")]
        cutoff_date = max(dates) if dates else "2026-06-18"

        metrics: Dict[str, Any] = {}
        if certified_metrics is not None:
            metrics = dict(certified_metrics)
        elif ckpt_path.exists():
            try:
                ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
                metrics = dict(ckpt.get("metrics") or {})
            except Exception:
                metrics = {}

        if not metrics:
            metrics = {
                "ece": round(float(calibration.get("ece_score", 0.0)), 4),
                "target_precision": round(float(calibration.get("target_precision", 0.96)), 4),
            }

        feature_schema = {
            "version": FEATURE_SCHEMA_VERSION,
            "feature_dim": FEATURE_DIM,
            "feature_names": list(FEATURE_NAMES),
        }

        manifest = {
            "bundle_name": "AdRadarBrainBundle",
            "bundle_version": bundle_version,
            "statute_cutoff_date": cutoff_date,
            "num_precedents": len(precedents),
            "embeddings_dim": precedent_embeddings.shape[1] if precedent_embeddings.ndim > 1 else 0,
            "encoder_model": index.encoder_model_name,
            "encoder_device": str(index.device),
            "use_fast_ngram_fallback": bool(index.use_fast_ngram_fallback),
            "onnx_model_size_bytes": len(onnx_bytes),
            "certified_metrics": metrics,
            "architecture": "LinkCentricAdClassifierV3",
            "feature_dim": FEATURE_DIM,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "author": "AdRadar AI Research Lab",
        }
        if manifest_extra:
            manifest.update(manifest_extra)

        return cls(
            onnx_bytes=onnx_bytes,
            calibration=calibration,
            precedents=precedents,
            precedent_embeddings=precedent_embeddings,
            manifest=manifest,
            feature_schema=feature_schema,
        )
