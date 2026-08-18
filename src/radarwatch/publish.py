"""Create the small, runtime-only public demo bundle."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from radarwatch.config import PipelineConfig
from radarwatch.contracts import MetricsContract, ProvenanceContract
from radarwatch.exceptions import DataValidationError
from radarwatch.utils import atomic_write_json, file_record, read_json, utc_now

MAX_DEMO_BYTES = 30 * 1024 * 1024


def _copy(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def _stage_runtimes(config: PipelineConfig) -> dict[str, float]:
    runtimes = {}
    for path in config.path("stages").glob("*.json"):
        record = read_json(path)
        if record.get("status") == "completed":
            runtimes[record["stage"]] = float(record.get("runtime_seconds", 0))
    return runtimes


def publish(config: PipelineConfig) -> list[Path]:
    detection = read_json(config.path("processed") / "detection" / "detection.json")
    evaluation = read_json(config.path("processed") / "evaluation" / "evaluation.json")
    impact = read_json(config.path("processed") / "impact" / "impact.json")
    prepare = read_json(config.path("interim") / "aligned" / "prepare.json")
    sources = read_json(config.path("raw") / "source_manifest.json")
    if evaluation["evaluation"]["exact_time"]["overlap_area_km2"] <= 0:
        raise DataValidationError("Publishing is blocked because exact-time overlap is zero")

    demo_dir = config.path("demo")
    demo_dir.mkdir(parents=True, exist_ok=True)
    metrics = MetricsContract(
        event={
            "id": config.event.id,
            "title": config.event.title,
            "area_name": config.event.area_name,
            "event_time": config.event.event_time.isoformat(),
            "bounds_wgs84": list(config.event.bounds_wgs84),
            "browse_bounds_wgs84": prepare["bounds_wgs84"],
            "analysis_crs": config.event.analysis_crs,
            "before_acquired_at": config.sar.before.acquired_at.isoformat(),
            "after_acquired_at": config.sar.after.acquired_at.isoformat(),
        },
        detection={
            "water_threshold_db": detection["water_threshold_db"],
            "water_threshold_method": detection["water_threshold_method"],
            "default": detection["scenarios"]["default"],
            "sensitivity": evaluation["sensitivity_against_exact_time"],
            "feature_count": detection["feature_count"],
        },
        infrastructure=impact["infrastructure"],
        isolation=impact["isolation"],
        evaluation=evaluation["evaluation"],
        runtime_seconds=_stage_runtimes(config),
        generated_at=utc_now(),
    )
    metrics_path = atomic_write_json(demo_dir / "metrics.json", metrics.model_dump(mode="json"))

    provenance = ProvenanceContract(
        event_id=config.event.id,
        sources=sources["sources"],
        processing={
            "software": "RadarWatch 0.1.0",
            "config_sha256": config.config_hash(),
            "analysis_crs": config.event.analysis_crs,
            "web_crs": "EPSG:4326",
            "resolution_m": config.event.resolution_m,
            "shape": prepare["shape"],
            "common_valid_coverage": prepare["common_valid_coverage"],
            "backscatter_units": prepare["backscatter_units"],
            "filter": prepare["filter"],
            "detection_method": "Multi-Otsu low-backscatter plus dual-polarization change rules",
        },
        generated_at=utc_now(),
    )
    provenance_path = atomic_write_json(
        demo_dir / "provenance.json", provenance.model_dump(mode="json")
    )

    copy_map = {
        config.path("processed") / "browse" / "before_vv.png": "before_vv.png",
        config.path("processed") / "browse" / "after_vv.png": "after_vv.png",
        config.path("processed") / "detection" / "flood_extent.geojson": "flood_extent.geojson",
        config.path("processed")
        / "evaluation"
        / "reference_exact_time.geojson": "reference_exact_time.geojson",
        config.path("processed")
        / "evaluation"
        / "reference_optical.geojson": "reference_optical.geojson",
        config.path("processed") / "impact" / "exposed_roads.geojson": "exposed_roads.geojson",
        config.path("processed")
        / "impact"
        / "exposed_railways.geojson": "exposed_railways.geojson",
        config.path("processed")
        / "impact"
        / "exposed_buildings.geojson": "exposed_buildings.geojson",
        config.path("processed")
        / "impact"
        / "exposed_critical_assets.geojson": "exposed_critical_assets.geojson",
        config.path("processed")
        / "impact"
        / "potentially_isolated_settlements.geojson": "potentially_isolated_settlements.geojson",
    }
    outputs = [metrics_path, provenance_path]
    for source, name in copy_map.items():
        if not source.exists():
            raise DataValidationError(f"Required publish artifact is missing: {source}")
        outputs.append(_copy(source, demo_dir / name))

    total_bytes = sum(path.stat().st_size for path in outputs)
    if total_bytes > MAX_DEMO_BYTES:
        raise DataValidationError(
            f"Demo bundle is {total_bytes / 1024 / 1024:.1f} MB; limit is 30 MB"
        )
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": config.event.id,
        "generated_at": utc_now(),
        "total_bytes": total_bytes,
        "assets": [file_record(path, demo_dir) for path in outputs],
    }
    manifest_path = atomic_write_json(demo_dir / "manifest.json", manifest)
    outputs.append(manifest_path)
    final_total_bytes = sum(path.stat().st_size for path in outputs)
    if final_total_bytes > MAX_DEMO_BYTES:
        raise DataValidationError(
            f"Demo bundle is {final_total_bytes / 1024 / 1024:.1f} MB; limit is 30 MB"
        )
    return outputs
