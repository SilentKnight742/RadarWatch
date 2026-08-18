"""Prepare aligned before/after OPERA RTC mosaics."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from rasterio.warp import transform_bounds

from radarwatch.config import PipelineConfig
from radarwatch.exceptions import DataValidationError
from radarwatch.raster import (
    linear_to_db,
    masked_median,
    mosaic_to_grid,
    target_grid,
    write_grayscale_png,
    write_raster,
)
from radarwatch.utils import atomic_write_json, utc_now


def _source_pairs(config: PipelineConfig, role: str, polarization: str) -> list[tuple[Path, Path]]:
    group = getattr(config.sar, role)
    directory = config.path("raw") / "sar"
    pairs = []
    for product in group.products:
        data = directory / f"{product}_{polarization}.tif"
        mask = directory / f"{product}_mask.tif"
        if not data.exists() or not mask.exists():
            raise DataValidationError(f"Missing {role} OPERA source: {data} or {mask}")
        pairs.append((data, mask))
    return pairs


def prepare(config: PipelineConfig) -> list[Path]:
    output_dir = config.path("interim") / "aligned"
    browse_dir = config.path("processed") / "browse"
    output_dir.mkdir(parents=True, exist_ok=True)
    browse_dir.mkdir(parents=True, exist_ok=True)
    transform, width, height = target_grid(
        config.event.bounds_wgs84,
        config.event.analysis_crs,
        config.event.resolution_m,
    )

    arrays: dict[str, np.ndarray] = {}
    valid_masks: list[np.ndarray] = []
    outputs: list[Path] = []
    for role in ("before", "after"):
        for polarization in config.sar.polarizations:
            linear, valid = mosaic_to_grid(
                _source_pairs(config, role, polarization),
                transform,
                config.event.analysis_crs,
                width,
                height,
            )
            db = linear_to_db(linear, valid)
            db = masked_median(db, config.detection.median_kernel)
            arrays[f"{role}_{polarization.lower()}"] = db
            valid_masks.append(np.isfinite(db))

    common_valid = np.logical_and.reduce(valid_masks)
    coverage = float(common_valid.mean())
    if coverage < 0.95:
        raise DataValidationError(
            f"Common valid SAR coverage is {coverage:.1%}; at least 95% is required"
        )
    for name, array in arrays.items():
        array[~common_valid] = np.nan
        output = write_raster(
            output_dir / f"{name}_db.tif",
            array,
            transform,
            config.event.analysis_crs,
            nodata=-9999.0,
            dtype="float32",
        )
        outputs.append(output)

    before_png = write_grayscale_png(browse_dir / "before_vv.png", arrays["before_vv"])
    after_png = write_grayscale_png(browse_dir / "after_vv.png", arrays["after_vv"])
    outputs.extend([before_png, after_png])

    projected_bounds = (
        transform.c,
        transform.f - height * config.event.resolution_m,
        transform.c + width * config.event.resolution_m,
        transform.f,
    )
    web_bounds = transform_bounds(
        config.event.analysis_crs, "EPSG:4326", *projected_bounds, densify_pts=21
    )
    metadata = atomic_write_json(
        output_dir / "prepare.json",
        {
            "event_id": config.event.id,
            "generated_at": utc_now(),
            "analysis_crs": config.event.analysis_crs,
            "resolution_m": config.event.resolution_m,
            "shape": [height, width],
            "transform": list(transform)[:6],
            "bounds_wgs84": list(web_bounds),
            "common_valid_coverage": coverage,
            "backscatter_units": "dB (10*log10 of OPERA RTC power)",
            "filter": f"{config.detection.median_kernel}x{config.detection.median_kernel} median",
        },
    )
    outputs.append(metadata)
    return outputs
