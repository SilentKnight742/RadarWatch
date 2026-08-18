"""Command-line interface for the RadarWatch pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from radarwatch.config import load_config
from radarwatch.exceptions import RadarWatchError
from radarwatch.pipeline import STAGES, run_pipeline

app = typer.Typer(
    name="radarwatch",
    help="Reproducible SAR flood-change and infrastructure-exposure pipeline.",
    no_args_is_help=True,
)


@app.command()
def validate(
    config: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)],
) -> None:
    """Validate configuration without making network requests."""
    parsed = load_config(config)
    typer.echo(f"Valid: {parsed.event.id}")
    typer.echo(f"Config SHA-256: {parsed.config_hash()}")


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)],
    from_stage: Annotated[str, typer.Option("--from-stage")] = "acquire",
    until_stage: Annotated[str, typer.Option("--until-stage")] = "publish",
    offline: Annotated[bool, typer.Option("--offline")] = False,
) -> None:
    """Run all or part of the staged pipeline."""
    if from_stage not in STAGES or until_stage not in STAGES:
        raise typer.BadParameter(f"Valid stages: {', '.join(STAGES)}")
    parsed = load_config(config)
    try:
        records = run_pipeline(
            parsed,
            from_stage=from_stage,
            until_stage=until_stage,
            offline=offline,
        )
    except (RadarWatchError, RuntimeError, ValueError) as exc:
        typer.echo(f"Pipeline failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for record in records:
        typer.echo(
            f"{record['stage']}: {record['status']} ({record.get('runtime_seconds', 0):.2f}s)"
        )


if __name__ == "__main__":
    app()
