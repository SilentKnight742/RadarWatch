"""Raster grid, filtering, and serialization primitives."""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_origin
from rasterio.warp import reproject, transform_bounds
from scipy.ndimage import generic_filter

from radarwatch.exceptions import DataValidationError


def linear_to_db(values: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    array = np.asarray(values, dtype=np.float32)
    output = np.full(array.shape, np.nan, dtype=np.float32)
    mask = np.isfinite(array) & (array > 0)
    if valid is not None:
        mask &= valid
    output[mask] = 10.0 * np.log10(array[mask])
    return output


def masked_median(values: np.ndarray, size: int) -> np.ndarray:
    if size == 1:
        return values.astype(np.float32, copy=True)

    def nan_median(window: np.ndarray) -> float:
        finite = window[np.isfinite(window)]
        return float(np.median(finite)) if finite.size else np.nan

    output = generic_filter(values, nan_median, size=size, mode="nearest").astype(np.float32)
    output[~np.isfinite(values)] = np.nan
    return output


def target_grid(
    bounds_wgs84: tuple[float, float, float, float], crs: str, resolution: float
) -> tuple[Affine, int, int]:
    west, south, east, north = transform_bounds("EPSG:4326", crs, *bounds_wgs84, densify_pts=21)
    left = math.floor(west / resolution) * resolution
    bottom = math.floor(south / resolution) * resolution
    right = math.ceil(east / resolution) * resolution
    top = math.ceil(north / resolution) * resolution
    width = round((right - left) / resolution)
    height = round((top - bottom) / resolution)
    return from_origin(left, top, resolution, resolution), width, height


def reproject_source(
    data_path: Path,
    mask_path: Path,
    transform: Affine,
    crs: str,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    destination = np.full((height, width), np.nan, dtype=np.float32)
    mask_destination = np.full((height, width), 255, dtype=np.uint8)
    with rasterio.open(data_path) as source:
        source_data = source.read(1).astype(np.float32)
        reproject(
            source=source_data,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=transform,
            dst_crs=crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    with rasterio.open(mask_path) as source:
        reproject(
            source=source.read(1),
            destination=mask_destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=transform,
            dst_crs=crs,
            dst_nodata=255,
            resampling=Resampling.nearest,
        )
    valid = np.isfinite(destination) & (destination > 0) & (mask_destination == 0)
    return destination, valid


def mosaic_to_grid(
    sources: Iterable[tuple[Path, Path]],
    transform: Affine,
    crs: str,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    total = np.zeros((height, width), dtype=np.float64)
    count = np.zeros((height, width), dtype=np.uint8)
    for data_path, mask_path in sources:
        data, valid = reproject_source(data_path, mask_path, transform, crs, width, height)
        total[valid] += data[valid]
        count[valid] += 1
    valid = count > 0
    if not valid.any():
        raise DataValidationError("No valid OPERA pixels intersect the configured AOI")
    mosaic = np.full((height, width), np.nan, dtype=np.float32)
    mosaic[valid] = (total[valid] / count[valid]).astype(np.float32)
    return mosaic, valid


def write_raster(
    path: Path,
    data: np.ndarray,
    transform: Affine,
    crs: str,
    *,
    nodata: float | int,
    dtype: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writable = np.asarray(data).copy()
    if np.issubdtype(writable.dtype, np.floating):
        writable[~np.isfinite(writable)] = nodata
    profile = {
        "driver": "COG",
        "height": writable.shape[0],
        "width": writable.shape[1],
        "count": 1,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 3 if dtype.startswith("float") else 2,
        "blocksize": 256,
        "overview_resampling": "nearest",
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(writable.astype(dtype), 1)
    return path


def write_grayscale_png(path: Path, data_db: np.ndarray, low: float = -25, high: float = 0) -> Path:
    clipped = np.clip((data_db - low) / (high - low), 0, 1)
    grayscale = (clipped * 255).astype(np.uint8)
    alpha = np.where(np.isfinite(data_db), 230, 0).astype(np.uint8)
    rgba = np.dstack([grayscale, grayscale, grayscale, alpha])
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)
    return path


def read_raster(path: Path) -> tuple[np.ndarray, dict]:
    with rasterio.open(path) as dataset:
        array = dataset.read(1).astype(np.float32)
        if dataset.nodata is not None:
            array[array == dataset.nodata] = np.nan
        profile = dataset.profile.copy()
        profile["bounds"] = dataset.bounds
    return array, profile
