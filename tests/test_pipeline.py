from pathlib import Path

from radarwatch import pipeline
from radarwatch.config import load_config


def test_stage_record_is_reused_only_while_output_hash_matches(tmp_path: Path, monkeypatch) -> None:
    base = load_config("configs/valencia.yaml")
    config = base.model_copy(
        update={"paths": base.paths.model_copy(update={"workspace": tmp_path})}
    )
    calls = 0

    def fake_acquire() -> list[Path]:
        nonlocal calls
        calls += 1
        output = config.path("raw") / "fixture.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"run-{calls}", encoding="utf-8")
        return [output]

    monkeypatch.setattr(pipeline, "acquire", lambda _config, offline=False: fake_acquire())

    first = pipeline.run_pipeline(config, until_stage="acquire")
    second = pipeline.run_pipeline(config, until_stage="acquire")
    assert calls == 1
    assert first[0]["outputs"] == second[0]["outputs"]

    (config.path("raw") / "fixture.txt").write_text("tampered", encoding="utf-8")
    pipeline.run_pipeline(config, until_stage="acquire")
    assert calls == 2
