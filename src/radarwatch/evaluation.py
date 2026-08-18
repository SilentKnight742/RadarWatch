"""Agreement metrics against operational Copernicus flood delineations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from shapely.geometry import box

from radarwatch.config import PipelineConfig, ReferenceConfig
from radarwatch.contracts import EvaluationMetrics
from radarwatch.exceptions import DataValidationError
from radarwatch.utils import atomic_write_json, utc_now


def binary_metrics(
    predicted: np.ndarray,
    reference: np.ndarray,
    valid: np.ndarray,
    pixel_area_km2: float,
    *,
    label: str,
    caveat: str,
    acquired_at: str,
) -> EvaluationMetrics:
    pred = predicted.astype(bool) & valid
    ref = reference.astype(bool) & valid
    tp = int(np.count_nonzero(pred & ref))
    fp = int(np.count_nonzero(pred & ~ref & valid))
    fn = int(np.count_nonzero(~pred & ref & valid))
    tn = int(np.count_nonzero(~pred & ~ref & valid))

    def ratio(numerator: int, denominator: int) -> float:
        return float(numerator / denominator) if denominator else 0.0

    detected_area = float(pred.sum() * pixel_area_km2)
    reference_area = float(ref.sum() * pixel_area_km2)
    return EvaluationMetrics(
        label=label,
        caveat=caveat,
        acquired_at=acquired_at,
        iou=ratio(tp, tp + fp + fn),
        dice_f1=ratio(2 * tp, 2 * tp + fp + fn),
        precision=ratio(tp, tp + fp),
        recall=ratio(tp, tp + fn),
        false_positive_rate=ratio(fp, fp + tn),
        detected_area_km2=detected_area,
        reference_area_km2=reference_area,
        area_difference_km2=detected_area - reference_area,
        valid_comparison_area_km2=float(valid.sum() * pixel_area_km2),
        overlap_area_km2=float(tp * pixel_area_km2),
    )


def _load_reference(
    config: PipelineConfig,
    name: str,
    reference: ReferenceConfig,
    shape: tuple[int, int],
    transform: rasterio.Affine,
) -> tuple[np.ndarray, gpd.GeoDataFrame]:
    path = config.path("raw") / "references" / f"cems_{name}.geojson"
    data = gpd.read_file(path)
    if "notation" in data.columns:
        data = data[data["notation"] == "Flooded area"].copy()
    aoi = gpd.GeoDataFrame(geometry=[box(*config.event.bounds_wgs84)], crs="EPSG:4326")
    data = gpd.clip(data.to_crs("EPSG:4326"), aoi)
    if data.empty:
        raise DataValidationError(f"Reference '{name}' has no flooded area inside AOI03")
    projected = data.to_crs(config.event.analysis_crs)
    mask = rasterize(
        ((geometry, 1) for geometry in projected.geometry if not geometry.is_empty),
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    ).astype(bool)
    if not mask.any():
        raise DataValidationError(f"Reference '{name}' rasterized to an empty mask")
    data["reference"] = name
    data["label"] = reference.label
    return mask, data


def evaluate(config: PipelineConfig) -> list[Path]:
    detection_dir = config.path("processed") / "detection"
    default_path = detection_dir / "evidence_default.tif"
    with rasterio.open(default_path) as dataset:
        evidence = dataset.read(1)
        transform = dataset.transform
        crs = dataset.crs
        shape = evidence.shape
    if str(crs) != config.event.analysis_crs:
        raise DataValidationError(f"Evidence CRS {crs} does not match {config.event.analysis_crs}")
    aligned_path = config.path("interim") / "aligned" / "after_vv_db.tif"
    with rasterio.open(aligned_path) as dataset:
        aligned = dataset.read(1)
        valid = np.isfinite(aligned) & (aligned != dataset.nodata)

    pixel_area_km2 = config.event.resolution_m**2 / 1_000_000
    evaluations: dict[str, Any] = {}
    outputs: list[Path] = []
    output_dir = config.path("processed") / "evaluation"
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_masks: dict[str, np.ndarray] = {}
    for name, reference in (
        ("exact_time", config.references.exact_time),
        ("optical", config.references.optical),
    ):
        reference_mask, vectors = _load_reference(config, name, reference, shape, transform)
        reference_masks[name] = reference_mask
        metrics = binary_metrics(
            evidence > 0,
            reference_mask,
            valid,
            pixel_area_km2,
            label=reference.label,
            caveat=reference.caveat,
            acquired_at=reference.acquired_at.isoformat(),
        )
        evaluations[name] = metrics.model_dump(mode="json")
        vector_path = output_dir / f"reference_{name}.geojson"
        vectors.to_file(vector_path, driver="GeoJSON")
        outputs.append(vector_path)

    if evaluations["exact_time"]["overlap_area_km2"] <= 0:
        raise DataValidationError(
            "Default detection has zero overlap with the exact-time Copernicus product; "
            "publishing would be misleading"
        )

    sensitivity: dict[str, Any] = {}
    for scenario in config.detection.scenarios:
        with rasterio.open(detection_dir / f"evidence_{scenario}.tif") as dataset:
            scenario_mask = dataset.read(1) > 0
        scenario_metrics = binary_metrics(
            scenario_mask,
            reference_masks["exact_time"],
            valid,
            pixel_area_km2,
            label=config.references.exact_time.label,
            caveat=config.references.exact_time.caveat,
            acquired_at=config.references.exact_time.acquired_at.isoformat(),
        )
        sensitivity[scenario] = {
            "detected_area_km2": scenario_metrics.detected_area_km2,
            "iou": scenario_metrics.iou,
            "precision": scenario_metrics.precision,
            "recall": scenario_metrics.recall,
        }

    summary = atomic_write_json(
        output_dir / "evaluation.json",
        {
            "event_id": config.event.id,
            "generated_at": utc_now(),
            "evaluation": evaluations,
            "sensitivity_against_exact_time": sensitivity,
            "terminology": "Agreement metrics, not field-validated accuracy",
        },
    )
    outputs.append(summary)
    return outputs
