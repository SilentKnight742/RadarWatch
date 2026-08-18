from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from pyproj import Transformer
from shapely.geometry import LineString, Point, box

from radarwatch.acquisition import PRODUCT_ID_PATTERN
from radarwatch.config import load_config
from radarwatch.contracts import MetricsContract, ProvenanceContract
from radarwatch.pipeline import STAGES, run_pipeline
from radarwatch.raster import target_grid, write_raster
from radarwatch.utils import atomic_write_json, read_json


def fixture_config(tmp_path: Path):
    config = load_config("configs/valencia.yaml")
    inverse = Transformer.from_crs("EPSG:32630", "EPSG:4326", always_xy=True)
    west, south = inverse.transform(720000, 4360000)
    east, north = inverse.transform(720600, 4360600)
    event = config.event.model_copy(update={"bounds_wgs84": (west, south, east, north)})
    detection = config.detection.model_copy(
        update={"water_threshold_db": -15.0, "min_component_pixels": 4}
    )
    paths = config.paths.model_copy(update={"workspace": tmp_path})
    return config.model_copy(update={"event": event, "detection": detection, "paths": paths})


def _catalogue_record(config, product: str) -> dict:
    match = PRODUCT_ID_PATTERN.fullmatch(product)
    assert match is not None
    track, burst, subswath, timestamp, _ = match.groups()
    start_time = (
        f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        f"T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
    )
    return {
        "sceneName": product,
        "pathNumber": config.sar.relative_orbit,
        "flightDirection": config.sar.flight_direction,
        "productVersion": config.sar.product_version,
        "polarization": config.sar.polarizations,
        "operaBurstID": f"{track}_{burst}_{subswath}",
        "startTime": start_time,
        "bytes": {},
        "additionalUrls": [],
    }


def write_synthetic_sources(config):
    transform, width, height = target_grid(
        config.event.bounds_wgs84, config.event.analysis_crs, config.event.resolution_m
    )
    flood_rows = slice(5, min(16, height - 2))
    flood_cols = slice(5, min(16, width - 2))
    raw = config.path("raw") / "sar"
    raw.mkdir(parents=True, exist_ok=True)
    for role, group in (("before", config.sar.before), ("after", config.sar.after)):
        for product in group.products:
            for polarization, dry_db, wet_db in (("VV", -10.0, -19.0), ("VH", -14.0, -21.0)):
                data = np.full((height, width), 10 ** (dry_db / 10), dtype=np.float32)
                if role == "after":
                    data[flood_rows, flood_cols] = 10 ** (wet_db / 10)
                write_raster(
                    raw / f"{product}_{polarization}.tif",
                    data,
                    transform,
                    config.event.analysis_crs,
                    nodata=0,
                    dtype="float32",
                )
            write_raster(
                raw / f"{product}_mask.tif",
                np.zeros((height, width), dtype=np.uint8),
                transform,
                config.event.analysis_crs,
                nodata=255,
                dtype="uint8",
            )
            atomic_write_json(raw / f"{product}.catalog.json", _catalogue_record(config, product))

    left = transform.c + flood_cols.start * config.event.resolution_m
    right = transform.c + flood_cols.stop * config.event.resolution_m
    top = transform.f - flood_rows.start * config.event.resolution_m
    bottom = transform.f - flood_rows.stop * config.event.resolution_m
    reference = gpd.GeoDataFrame(
        {"notation": ["Flooded area"], "area": [((right - left) * (top - bottom)) / 10_000]},
        geometry=[box(left, bottom, right, top)],
        crs=config.event.analysis_crs,
    ).to_crs("EPSG:4326")
    reference_dir = config.path("raw") / "references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    reference.to_file(reference_dir / "cems_exact_time.geojson", driver="GeoJSON")
    reference.to_file(reference_dir / "cems_optical.geojson", driver="GeoJSON")
    return box(left, bottom, right, top)


def write_synthetic_osm(config, flood_geometry) -> None:
    west, south, east, north = config.event.bounds_wgs84
    outer = (
        gpd.GeoSeries([box(west, south, east, north)], crs="EPSG:4326")
        .to_crs(config.event.analysis_crs)[0]
        .buffer(config.impact.osm_buffer_m)
    )
    exit_point = outer.boundary.interpolate(
        outer.boundary.project(Point(outer.bounds[0], outer.centroid.y))
    )
    settlement_point = Point(flood_geometry.bounds[2] + 120, flood_geometry.centroid.y)
    flooded_node = flood_geometry.centroid
    route_parts = [
        LineString([exit_point, flooded_node]),
        LineString([flooded_node, settlement_point]),
    ]

    graph = nx.MultiDiGraph(crs=config.event.analysis_crs)
    for node, point in enumerate((exit_point, flooded_node, settlement_point)):
        graph.add_node(node, x=point.x, y=point.y)
    for u, v, geometry in ((0, 1, route_parts[0]), (1, 2, route_parts[1])):
        graph.add_edge(u, v, geometry=geometry, length=geometry.length, highway="residential")
        graph.add_edge(v, u, geometry=LineString(geometry.coords[::-1]), length=geometry.length)

    osm_dir = config.path("raw") / "osm"
    osm_dir.mkdir(parents=True, exist_ok=True)
    roads = gpd.GeoDataFrame(
        {
            "u": [0, 1],
            "v": [1, 2],
            "key": [0, 0],
            "osmid": [1001, 1002],
            "name": ["Fixture Road", "Fixture Road"],
            "highway": ["residential", "residential"],
            "length": [geometry.length for geometry in route_parts],
        },
        geometry=route_parts,
        crs=config.event.analysis_crs,
    )
    rail = LineString(
        [
            (flood_geometry.bounds[0], flood_geometry.centroid.y),
            (flood_geometry.bounds[2], flood_geometry.centroid.y),
        ]
    )
    railways = gpd.GeoDataFrame(
        {"osmid": [2001], "name": ["Fixture Rail"], "railway": ["rail"]},
        geometry=[rail],
        crs=config.event.analysis_crs,
    )
    buildings = gpd.GeoDataFrame(
        {"osmid": [3001], "name": ["Fixture Building"], "building": ["yes"]},
        geometry=[flooded_node.buffer(20)],
        crs=config.event.analysis_crs,
    )
    assets = gpd.GeoDataFrame(
        {"osmid": [4001], "name": ["Fixture Clinic"], "amenity": ["clinic"]},
        geometry=[flooded_node],
        crs=config.event.analysis_crs,
    )
    settlements = gpd.GeoDataFrame(
        {"osmid": [5001], "name": ["Fixtureville"], "place": ["village"]},
        geometry=[settlement_point],
        crs=config.event.analysis_crs,
    )
    for name, frame in (
        ("roads", roads),
        ("railways", railways),
        ("buildings", buildings),
        ("critical_assets", assets),
        ("settlements", settlements),
    ):
        frame.to_parquet(osm_dir / f"{name}.parquet", index=False)
    ox.save_graphml(graph, osm_dir / "drive.graphml")


def test_network_free_end_to_end_all_stages(tmp_path: Path) -> None:
    config = fixture_config(tmp_path)
    config.ensure_directories()
    flood_geometry = write_synthetic_sources(config)
    write_synthetic_osm(config, flood_geometry)

    records = run_pipeline(config, offline=True)

    assert [record["stage"] for record in records] == list(STAGES)
    assert all(record["status"] == "completed" for record in records)
    assert all("software_version" in record for record in records)
    assert all("inputs" in record for record in records)
    preparation = read_json(config.path("interim") / "aligned" / "prepare.json")
    detection = read_json(config.path("processed") / "detection" / "detection.json")
    evaluation = read_json(config.path("processed") / "evaluation" / "evaluation.json")
    metrics = MetricsContract.model_validate(read_json(config.path("demo") / "metrics.json"))
    ProvenanceContract.model_validate(read_json(config.path("demo") / "provenance.json"))
    manifest = read_json(config.path("demo") / "manifest.json")
    assert preparation["common_valid_coverage"] == 1.0
    assert detection["scenarios"]["default"]["detected_area_km2"] > 0
    assert evaluation["evaluation"]["exact_time"]["iou"] > 0.5
    assert evaluation["evaluation"]["optical"]["overlap_area_km2"] > 0
    assert metrics.infrastructure["exposed_buildings"] == 1
    assert metrics.isolation["settlements_evaluated"] == 1
    assert metrics.runtime_seconds["total"] > 0
    assert manifest["total_bytes"] < 30 * 1024 * 1024
