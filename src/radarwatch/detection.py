"""Classical dual-polarization flood/change evidence detection."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask, shapes
from shapely.geometry import shape
from skimage.filters import threshold_multiotsu
from skimage.morphology import (
    closing,
    footprint_rectangle,
    remove_small_holes,
    remove_small_objects,
)

from radarwatch.config import DetectionScenario, PipelineConfig
from radarwatch.exceptions import DetectionError
from radarwatch.raster import read_raster, write_raster
from radarwatch.utils import atomic_write_json, utc_now


def derive_water_threshold(
    post_vv: np.ndarray,
    bounds: tuple[float, float],
    override: float | None = None,
) -> tuple[float, str]:
    if override is not None:
        return float(override), "config_override"
    values = post_vv[np.isfinite(post_vv)]
    if values.size < 256 or np.unique(values).size < 3:
        raise DetectionError(
            "Multi-Otsu requires at least 256 valid pixels and three distinct values; "
            "set detection.water_threshold_db only for diagnosis"
        )
    try:
        threshold = float(threshold_multiotsu(values, classes=3)[0])
    except ValueError as exc:
        raise DetectionError(f"Unable to derive Multi-Otsu water threshold: {exc}") from exc
    return float(np.clip(threshold, bounds[0], bounds[1])), "multi_otsu_3class_clamped"


def classify_evidence(
    post_vv: np.ndarray,
    delta_vv: np.ndarray,
    delta_vh: np.ndarray,
    water_threshold: float,
    scenario: DetectionScenario,
    min_component_pixels: int,
) -> tuple[np.ndarray, np.ndarray]:
    valid = np.isfinite(post_vv) & np.isfinite(delta_vv) & np.isfinite(delta_vh)
    low_post = valid & (post_vv <= water_threshold)
    vv_drop = valid & (delta_vv <= scenario.vv_drop_db)
    vh_drop = valid & (delta_vh <= scenario.vh_drop_db)
    high = low_post & vv_drop & vh_drop
    combined = low_post & (vv_drop | vh_drop)
    combined = closing(combined, footprint=footprint_rectangle((3, 3)))
    maximum_small_size = max(min_component_pixels - 1, 0)
    combined = remove_small_holes(combined, max_size=maximum_small_size)
    combined = remove_small_objects(combined, max_size=maximum_small_size)
    high &= combined
    evidence = np.zeros(post_vv.shape, dtype=np.uint8)
    evidence[combined] = 1
    evidence[high] = 2
    return evidence, combined


def _vectorize_evidence(
    evidence: np.ndarray, transform: rasterio.Affine, crs: str
) -> gpd.GeoDataFrame:
    combined = evidence > 0
    records = []
    for index, (geometry, value) in enumerate(
        shapes(combined.astype(np.uint8), mask=combined, transform=transform), start=1
    ):
        if value != 1:
            continue
        polygon = shape(geometry)
        region_mask = (
            geometry_mask([geometry], out_shape=evidence.shape, transform=transform, invert=True)
            & combined
        )
        high_fraction = float((evidence[region_mask] == 2).mean())
        records.append(
            {
                "feature_id": f"flood-{index:04d}",
                "evidence": "high" if high_fraction >= 0.5 else "moderate",
                "high_fraction": round(high_fraction, 4),
                "area_km2": polygon.area / 1_000_000,
                "source": "RadarWatch classical SAR change evidence",
                "geometry": polygon,
            }
        )
    if not records:
        raise DetectionError("Detection produced no flood/change evidence polygons")
    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs)


def detect(config: PipelineConfig) -> list[Path]:
    aligned = config.path("interim") / "aligned"
    before_vv, before_profile = read_raster(aligned / "before_vv_db.tif")
    before_vh, _ = read_raster(aligned / "before_vh_db.tif")
    after_vv, after_profile = read_raster(aligned / "after_vv_db.tif")
    after_vh, _ = read_raster(aligned / "after_vh_db.tif")
    if not (
        before_vv.shape == before_vh.shape == after_vv.shape == after_vh.shape
        and before_profile["transform"] == after_profile["transform"]
        and before_profile["crs"] == after_profile["crs"]
    ):
        raise DetectionError("Aligned before/after rasters do not share one grid")

    delta_vv = after_vv - before_vv
    delta_vh = after_vh - before_vh
    threshold, threshold_method = derive_water_threshold(
        after_vv,
        config.detection.water_threshold_bounds_db,
        config.detection.water_threshold_db,
    )
    output_dir = config.path("processed") / "detection"
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    scenario_summary: dict[str, dict[str, float]] = {}
    default_evidence: np.ndarray | None = None
    pixel_area_km2 = config.event.resolution_m**2 / 1_000_000
    for name, scenario in config.detection.scenarios.items():
        evidence, combined = classify_evidence(
            after_vv,
            delta_vv,
            delta_vh,
            threshold,
            scenario,
            config.detection.min_component_pixels,
        )
        output = write_raster(
            output_dir / f"evidence_{name}.tif",
            evidence,
            after_profile["transform"],
            str(after_profile["crs"]),
            nodata=0,
            dtype="uint8",
        )
        outputs.append(output)
        scenario_summary[name] = {
            "vv_drop_db": scenario.vv_drop_db,
            "vh_drop_db": scenario.vh_drop_db,
            "detected_area_km2": float(combined.sum() * pixel_area_km2),
            "high_confidence_area_km2": float((evidence == 2).sum() * pixel_area_km2),
        }
        if name == "default":
            default_evidence = evidence
    if default_evidence is None or not np.any(default_evidence):
        raise DetectionError("The default scenario produced no flood/change evidence")

    vectors = _vectorize_evidence(
        default_evidence, after_profile["transform"], str(after_profile["crs"])
    )
    full_path = output_dir / "flood_extent.gpkg"
    vectors.to_file(full_path, layer="flood_extent", driver="GPKG")
    outputs.append(full_path)
    web_vectors = vectors.to_crs("EPSG:4326")
    web_vectors["geometry"] = web_vectors.geometry.simplify(0.00005, preserve_topology=True)
    web_path = output_dir / "flood_extent.geojson"
    web_vectors.to_file(web_path, driver="GeoJSON")
    outputs.append(web_path)

    metadata = atomic_write_json(
        output_dir / "detection.json",
        {
            "event_id": config.event.id,
            "generated_at": utc_now(),
            "water_threshold_db": threshold,
            "water_threshold_method": threshold_method,
            "classification": {
                "moderate": "low post-event VV and either VV or VH decrease",
                "high": "low post-event VV and both VV and VH decrease",
            },
            "min_component_pixels": config.detection.min_component_pixels,
            "scenarios": scenario_summary,
            "feature_count": len(vectors),
        },
    )
    outputs.append(metadata)
    return outputs
