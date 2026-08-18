import hashlib
import json
from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).parents[1] / "app.py"


def test_app_avoids_removed_streamlit_width_api() -> None:
    assert "use_container_width" not in APP_PATH.read_text(encoding="utf-8")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def make_demo(path: Path) -> None:
    path.mkdir(parents=True)
    Image.new("RGBA", (16, 16), (70, 70, 70, 220)).save(path / "before_vv.png")
    Image.new("RGBA", (16, 16), (35, 35, 35, 220)).save(path / "after_vv.png")
    empty = {"type": "FeatureCollection", "features": []}
    flood = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"evidence": "high"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-0.50, 39.40],
                            [-0.49, 39.40],
                            [-0.49, 39.41],
                            [-0.50, 39.41],
                            [-0.50, 39.40],
                        ]
                    ],
                },
            }
        ],
    }
    for name in (
        "reference_exact_time.geojson",
        "reference_optical.geojson",
        "exposed_roads.geojson",
        "exposed_railways.geojson",
        "exposed_buildings.geojson",
        "exposed_critical_assets.geojson",
        "potentially_isolated_settlements.geojson",
    ):
        write_json(path / name, empty)
    write_json(path / "flood_extent.geojson", flood)
    evaluation = {
        "label": "Operational-product agreement",
        "caveat": "Not independent ground truth.",
        "acquired_at": "2024-10-31T18:02:00Z",
        "iou": 0.4,
        "dice_f1": 0.57,
        "precision": 0.6,
        "recall": 0.55,
        "false_positive_rate": 0.02,
        "detected_area_km2": 1.2,
        "reference_area_km2": 1.1,
        "area_difference_km2": 0.1,
        "valid_comparison_area_km2": 100,
        "overlap_area_km2": 0.7,
    }
    write_json(
        path / "metrics.json",
        {
            "event": {
                "title": "Valencia Flood, Horta Sud",
                "bounds_wgs84": [-0.58, 39.36, -0.39, 39.50],
            },
            "detection": {
                "sensitivity": {
                    "strict": {"iou": 0.3, "detected_area_km2": 0.8},
                    "default": {"iou": 0.4, "detected_area_km2": 1.2},
                    "lenient": {"iou": 0.35, "detected_area_km2": 1.8},
                }
            },
            "infrastructure": {
                "detected_flood_area_km2": 1.2,
                "exposed_road_overlap_km": 4.2,
                "exposed_buildings": 12,
                "exposed_critical_assets": 1,
            },
            "isolation": {
                "potentially_isolated_count": 0,
                "potentially_isolated_names": [],
                "settlements_evaluated": 3,
                "caveat": "Screening signal only.",
            },
            "evaluation": {"exact_time": evaluation, "optical": evaluation},
        },
    )
    write_json(path / "provenance.json", {"event_id": "fixture", "sources": []})
    assets = []
    for asset_path in path.iterdir():
        if asset_path.name == "manifest.json":
            continue
        content = asset_path.read_bytes()
        assets.append(
            {
                "path": asset_path.name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    write_json(path / "manifest.json", {"assets": assets})


def test_app_renders_precomputed_case_study(tmp_path: Path, monkeypatch) -> None:
    demo = tmp_path / "demo"
    make_demo(demo)
    monkeypatch.setenv("RADARWATCH_DEMO_DIR", str(demo))

    app = AppTest.from_file(APP_PATH).run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "RadarWatch: Valencia 2024"
    assert len(app.metric) == 5
    assert len(app.tabs) == 4
    assert len(app.warning) == 1
    assert len(app.download_button) == 3
    assert not app.error


def test_app_explains_missing_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RADARWATCH_DEMO_DIR", str(tmp_path / "missing"))

    app = AppTest.from_file(APP_PATH).run(timeout=30)

    assert not app.exception
    assert "precomputed Valencia demo bundle is not present" in app.error[0].value


def test_app_explains_corrupt_bundle(tmp_path: Path, monkeypatch) -> None:
    demo = tmp_path / "demo"
    make_demo(demo)
    (demo / "metrics.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RADARWATCH_DEMO_DIR", str(demo))

    app = AppTest.from_file(APP_PATH).run(timeout=30)

    assert not app.exception
    assert "precomputed demo bundle is corrupt" in app.error[0].value
