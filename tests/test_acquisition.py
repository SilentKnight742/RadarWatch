import json
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pytest
from shapely.geometry import LineString, Point, box

from radarwatch import acquisition
from radarwatch.config import load_config
from radarwatch.exceptions import DataValidationError


def catalogue_record(config, product_id: str) -> dict:
    match = acquisition.PRODUCT_ID_PATTERN.fullmatch(product_id)
    assert match is not None
    track, burst_number, subswath, timestamp, _ = match.groups()
    return {
        "sceneName": product_id,
        "pathNumber": config.sar.relative_orbit,
        "flightDirection": config.sar.flight_direction,
        "productVersion": config.sar.product_version,
        "polarization": config.sar.polarizations,
        "operaBurstID": f"{track}_{burst_number}_{subswath}",
        "startTime": (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            f"T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
        ),
    }


def test_catalogue_record_contract() -> None:
    config = load_config("configs/valencia.yaml")
    product_id = config.sar.before.products[0]

    acquisition.validate_opera_record(catalogue_record(config, product_id), product_id, config)

    invalid = catalogue_record(config, product_id)
    invalid["pathNumber"] = 999
    with pytest.raises(DataValidationError, match="does not match"):
        acquisition.validate_opera_record(invalid, product_id, config)

    invalid = catalogue_record(config, product_id)
    invalid["operaBurstID"] = "T103_000000_IW3"
    with pytest.raises(DataValidationError, match="operaBurstID"):
        acquisition.validate_opera_record(invalid, product_id, config)


def test_search_requires_exactly_one_feature(monkeypatch) -> None:
    monkeypatch.setattr(acquisition, "_request_json", lambda *args, **kwargs: {"features": []})

    with pytest.raises(acquisition.AcquisitionError, match="Expected one"):
        acquisition.search_opera_product("missing")


def test_reference_download_and_offline_reuse(tmp_path: Path, monkeypatch) -> None:
    config = load_config("configs/valencia.yaml")
    target_dir = tmp_path / "references"
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"notation": "Flooded area"},
                "geometry": {"type": "Point", "coordinates": [0, 0]},
            }
        ],
    }
    monkeypatch.setattr(acquisition, "_request_json", lambda *args, **kwargs: payload)

    path, source = acquisition.acquire_reference(
        config.references.optical,
        "optical",
        target_dir,
        workspace=tmp_path,
    )
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert source["asset"]["path"] == "references/cems_optical.geojson"

    monkeypatch.setattr(
        acquisition,
        "_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    reused, _ = acquisition.acquire_reference(
        config.references.optical,
        "optical",
        target_dir,
        offline=True,
        workspace=tmp_path,
    )
    assert reused == path


def test_stream_download_uses_authenticated_session(tmp_path: Path) -> None:
    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"authenticated payload"

    class Session:
        def get(self, url, **kwargs):
            assert url == "https://example.test/asset.tif"
            assert kwargs["stream"] is True
            return Response()

    target = tmp_path / "asset.tif"
    acquisition._stream_download(Session(), "https://example.test/asset.tif", target, 21)
    assert target.read_bytes() == b"authenticated payload"


def test_osm_acquisition_with_mocked_overpass_and_offline_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    base = load_config("configs/valencia.yaml")
    config = base.model_copy(
        update={"paths": base.paths.model_copy(update={"workspace": tmp_path})}
    )
    features = gpd.GeoDataFrame(
        {
            "osmid": [1, 2, 3, 4],
            "name": ["Building", "Rail", "Clinic", "Fixtureville"],
            "building": ["yes", None, None, None],
            "railway": [None, "rail", None, None],
            "amenity": [None, None, "clinic", None],
            "place": [None, None, None, "village"],
        },
        geometry=[
            box(-0.51, 39.40, -0.509, 39.401),
            LineString([(-0.52, 39.40), (-0.49, 39.40)]),
            Point(-0.50, 39.40),
            Point(-0.49, 39.41),
        ],
        crs="EPSG:4326",
    )
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    graph.add_node(1, x=-0.52, y=39.40)
    graph.add_node(2, x=-0.49, y=39.41)
    graph.add_edge(
        1,
        2,
        osmid=10,
        name="Fixture Road",
        highway="residential",
        length=3000.0,
        geometry=LineString([(-0.52, 39.40), (-0.49, 39.41)]),
    )
    monkeypatch.setattr(ox, "features_from_bbox", lambda *args, **kwargs: features)
    monkeypatch.setattr(ox, "graph_from_bbox", lambda *args, **kwargs: graph)

    outputs, source = acquisition.acquire_osm(config)

    assert len(outputs) == 6
    assert all(path.exists() for path in outputs)
    assert source["license"] == "ODbL"

    monkeypatch.setattr(
        ox,
        "features_from_bbox",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    reused, _ = acquisition.acquire_osm(config, offline=True)
    assert reused == outputs
