"""Validated configuration and path resolution for RadarWatch."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from radarwatch.exceptions import ConfigurationError

BURST_KEY_PATTERN = re.compile(r"RTC-S1_(T\d{3}-\d+-IW\d)_")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventConfig(StrictModel):
    id: str
    title: str
    event_time: datetime
    area_name: str
    bounds_wgs84: tuple[float, float, float, float]
    analysis_crs: str = "EPSG:32630"
    resolution_m: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> EventConfig:
        west, south, east, north = self.bounds_wgs84
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bounds_wgs84 must be [west, south, east, north]")
        return self


class AcquisitionGroup(StrictModel):
    acquired_at: datetime
    products: list[str] = Field(min_length=1)


class SarConfig(StrictModel):
    dataset: Literal["OPERA-S1"]
    relative_orbit: int
    flight_direction: Literal["ASCENDING", "DESCENDING"]
    product_version: str
    polarizations: list[Literal["VV", "VH"]] = Field(min_length=2, max_length=2)
    before: AcquisitionGroup
    after: AcquisitionGroup

    @model_validator(mode="after")
    def validate_pairs(self) -> SarConfig:
        if set(self.polarizations) != {"VV", "VH"}:
            raise ValueError("V1 requires both VV and VH")
        if len(self.before.products) != len(self.after.products):
            raise ValueError("before and after must contain the same number of bursts")
        before_bursts = [BURST_KEY_PATTERN.search(product) for product in self.before.products]
        after_bursts = [BURST_KEY_PATTERN.search(product) for product in self.after.products]
        if any(match is None for match in before_bursts + after_bursts):
            raise ValueError("all SAR products must contain an OPERA burst identifier")
        if [match.group(1) for match in before_bursts if match] != [
            match.group(1) for match in after_bursts if match
        ]:
            raise ValueError("before and after must contain the same ordered burst IDs")
        return self


class DetectionScenario(StrictModel):
    vv_drop_db: float
    vh_drop_db: float


class DetectionConfig(StrictModel):
    water_threshold_db: float | None = None
    water_threshold_bounds_db: tuple[float, float] = (-20.0, -12.0)
    median_kernel: int = Field(default=3, ge=1)
    min_component_pixels: int = Field(default=10, ge=1)
    scenarios: dict[str, DetectionScenario]

    @model_validator(mode="after")
    def validate_detection(self) -> DetectionConfig:
        if self.median_kernel % 2 == 0:
            raise ValueError("median_kernel must be odd")
        if "default" not in self.scenarios:
            raise ValueError("detection.scenarios must contain 'default'")
        low, high = self.water_threshold_bounds_db
        if low >= high:
            raise ValueError("water_threshold_bounds_db must be increasing")
        return self


class ReferenceConfig(StrictModel):
    label: str
    acquired_at: datetime
    url: str
    caveat: str


class ReferencesConfig(StrictModel):
    exact_time: ReferenceConfig
    optical: ReferenceConfig


class ImpactConfig(StrictModel):
    osm_buffer_m: float = Field(gt=0)
    boundary_exit_tolerance_m: float = Field(gt=0)
    min_linear_overlap_m: float = Field(gt=0)
    building_overlap_fraction: float = Field(gt=0, le=1)
    place_tags: list[str] = Field(min_length=1)
    critical_amenities: list[str] = Field(min_length=1)


class PathsConfig(StrictModel):
    workspace: Path
    raw: Path
    interim: Path
    processed: Path
    stages: Path
    demo: Path


class PipelineConfig(StrictModel):
    event: EventConfig
    sar: SarConfig
    detection: DetectionConfig
    references: ReferencesConfig
    impact: ImpactConfig
    paths: PathsConfig
    config_path: Path = Field(exclude=True)

    def config_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"config_path"})
        # The resolved workspace is machine-specific and does not change the analysis.
        payload["paths"].pop("workspace", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def workspace(self) -> Path:
        return self.paths.workspace

    def path(self, name: Literal["raw", "interim", "processed", "stages", "demo"]) -> Path:
        return self.paths.workspace / getattr(self.paths, name)

    def ensure_directories(self) -> None:
        for name in ("raw", "interim", "processed", "stages", "demo"):
            self.path(name).mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path) -> PipelineConfig:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise ConfigurationError(f"Configuration does not exist: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("top-level YAML value must be a mapping")
        workspace_value = Path(raw["paths"]["workspace"])
        workspace = (config_path.parent / workspace_value).resolve()
        raw["paths"]["workspace"] = workspace
        return PipelineConfig.model_validate({**raw, "config_path": config_path})
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration {config_path}: {exc}") from exc
