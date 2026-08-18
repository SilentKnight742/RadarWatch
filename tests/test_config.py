from pathlib import Path

from radarwatch.config import load_config


def test_valencia_config_is_decision_complete(tmp_path: Path) -> None:
    config = load_config(Path("configs/valencia.yaml"))

    assert config.event.id == "valencia-2024-horta-sud"
    assert config.event.bounds_wgs84 == (-0.58, 39.36, -0.39, 39.50)
    assert config.event.analysis_crs == "EPSG:32630"
    assert config.sar.relative_orbit == 103
    assert len(config.sar.before.products) == len(config.sar.after.products) == 2
    assert set(config.sar.polarizations) == {"VV", "VH"}
    assert config.workspace == Path.cwd()
    assert len(config.config_hash()) == 64
    relocated = config.model_copy(
        update={"paths": config.paths.model_copy(update={"workspace": tmp_path})}
    )
    assert relocated.config_hash() == config.config_hash()
