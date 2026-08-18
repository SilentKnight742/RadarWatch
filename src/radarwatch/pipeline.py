"""Deterministic staged pipeline orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from radarwatch import __version__
from radarwatch.acquisition import acquire
from radarwatch.config import PipelineConfig
from radarwatch.detection import detect
from radarwatch.evaluation import evaluate
from radarwatch.impact import impact
from radarwatch.prepare import prepare
from radarwatch.publish import publish
from radarwatch.utils import atomic_write_json, file_record, read_json, sha256_file, utc_now

STAGES = ("acquire", "prepare", "detect", "evaluate", "impact", "publish")


def _record_path(config: PipelineConfig, stage: str) -> Path:
    return config.path("stages") / f"{stage}.json"


def _dependency_record(config: PipelineConfig, stage: str) -> dict[str, str] | None:
    index = STAGES.index(stage)
    if index == 0:
        return None
    path = _record_path(config, STAGES[index - 1])
    return {"path": str(path.relative_to(config.workspace)), "sha256": sha256_file(path)}


def _input_records(config: PipelineConfig, stage: str) -> list[dict[str, Any]]:
    index = STAGES.index(stage)
    if index == 0:
        return []
    previous = read_json(_record_path(config, STAGES[index - 1]))
    return previous.get("outputs", [])


def stage_is_current(config: PipelineConfig, stage: str) -> bool:
    path = _record_path(config, stage)
    if not path.exists():
        return False
    record = read_json(path)
    if record.get("status") != "completed" or record.get("config_sha256") != config.config_hash():
        return False
    expected_dependency = _dependency_record(config, stage)
    if record.get("dependency") != expected_dependency:
        return False
    if record.get("software_version") != __version__:
        return False
    if record.get("inputs") != _input_records(config, stage):
        return False
    for output in record.get("outputs", []):
        output_path = config.workspace / output["path"]
        if not output_path.exists() or sha256_file(output_path) != output["sha256"]:
            return False
    return True


def _execute_stage(
    config: PipelineConfig,
    stage: str,
    function: Callable[[], list[Path]],
) -> dict[str, Any]:
    dependency = _dependency_record(config, stage)
    inputs = _input_records(config, stage)
    started = time.perf_counter()
    try:
        outputs = function()
        runtime = time.perf_counter() - started
        if stage == "publish":
            metrics_path = config.path("demo") / "metrics.json"
            metrics = read_json(metrics_path)
            metrics["runtime_seconds"]["publish"] = runtime
            metrics["runtime_seconds"]["total"] = sum(
                value for key, value in metrics["runtime_seconds"].items() if key != "total"
            )
            atomic_write_json(metrics_path, metrics)
            manifest_path = config.path("demo") / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["assets"] = [
                file_record(config.path("demo") / asset["path"], config.path("demo"))
                for asset in manifest["assets"]
            ]
            manifest["total_bytes"] = sum(asset["bytes"] for asset in manifest["assets"])
            atomic_write_json(manifest_path, manifest)
        record = {
            "schema_version": "1.0",
            "stage": stage,
            "status": "completed",
            "generated_at": utc_now(),
            "runtime_seconds": runtime,
            "software_version": __version__,
            "config_sha256": config.config_hash(),
            "dependency": dependency,
            "inputs": inputs,
            "outputs": [file_record(path, config.workspace) for path in outputs],
        }
        atomic_write_json(_record_path(config, stage), record)
        return record
    except Exception as exc:
        atomic_write_json(
            _record_path(config, stage),
            {
                "schema_version": "1.0",
                "stage": stage,
                "status": "failed",
                "generated_at": utc_now(),
                "runtime_seconds": time.perf_counter() - started,
                "software_version": __version__,
                "config_sha256": config.config_hash(),
                "dependency": dependency,
                "inputs": inputs,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise


def run_pipeline(
    config: PipelineConfig,
    *,
    from_stage: str = "acquire",
    until_stage: str = "publish",
    offline: bool = False,
) -> list[dict[str, Any]]:
    if from_stage not in STAGES or until_stage not in STAGES:
        raise ValueError(f"Stages must be one of: {', '.join(STAGES)}")
    start = STAGES.index(from_stage)
    end = STAGES.index(until_stage)
    if start > end:
        raise ValueError("from_stage must not come after until_stage")
    config.ensure_directories()

    for previous in STAGES[:start]:
        if not stage_is_current(config, previous):
            raise RuntimeError(
                f"Cannot start from '{from_stage}': prerequisite stage '{previous}' is not current"
            )

    functions: dict[str, Callable[[], list[Path]]] = {
        "acquire": lambda: acquire(config, offline=offline),
        "prepare": lambda: prepare(config),
        "detect": lambda: detect(config),
        "evaluate": lambda: evaluate(config),
        "impact": lambda: impact(config),
        "publish": lambda: publish(config),
    }
    records = []
    for stage in STAGES[start : end + 1]:
        if stage_is_current(config, stage):
            records.append(read_json(_record_path(config, stage)))
            continue
        records.append(_execute_stage(config, stage, functions[stage]))
    return records
