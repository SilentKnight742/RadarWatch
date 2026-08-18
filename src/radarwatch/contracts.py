"""Published JSON contracts used by the pipeline and app."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationMetrics(ContractModel):
    label: str
    caveat: str
    acquired_at: str
    iou: float = Field(ge=0, le=1)
    dice_f1: float = Field(ge=0, le=1)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    detected_area_km2: float = Field(ge=0)
    reference_area_km2: float = Field(ge=0)
    area_difference_km2: float
    valid_comparison_area_km2: float = Field(gt=0)
    overlap_area_km2: float = Field(ge=0)


class MetricsContract(ContractModel):
    schema_version: str = "1.0"
    event: dict[str, Any]
    detection: dict[str, Any]
    infrastructure: dict[str, Any]
    isolation: dict[str, Any]
    evaluation: dict[str, EvaluationMetrics]
    runtime_seconds: dict[str, float]
    generated_at: str


class ProvenanceContract(ContractModel):
    schema_version: str = "1.0"
    event_id: str
    sources: list[dict[str, Any]]
    processing: dict[str, Any]
    generated_at: str
