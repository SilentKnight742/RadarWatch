"""Infrastructure exposure and conservative network-isolation analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from radarwatch.config import PipelineConfig
from radarwatch.exceptions import ImpactAnalysisError
from radarwatch.utils import atomic_write_json, utc_now, write_feature_collection

ROAD_CLASSES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
}


def _safe_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _normalized_road_class(value: Any) -> str:
    candidate = (_safe_text(value) or "unknown").split(",", maxsplit=1)[0].strip()
    return candidate if candidate in ROAD_CLASSES else "other"


def line_exposure(
    lines: gpd.GeoDataFrame, flood_geometry: Any, minimum_overlap_m: float
) -> gpd.GeoDataFrame:
    if lines.empty:
        return lines.copy()
    result = lines.copy()
    result["overlap_m"] = result.geometry.apply(
        lambda geometry: (
            float(geometry.intersection(flood_geometry).length)
            if geometry is not None and not geometry.is_empty
            else 0.0
        )
    )
    return result[result["overlap_m"] >= minimum_overlap_m].copy()


def polygon_exposure(
    features: gpd.GeoDataFrame, flood_geometry: Any, overlap_fraction: float
) -> gpd.GeoDataFrame:
    if features.empty:
        return features.copy()
    result = features.copy()

    def measurements(geometry: Any) -> tuple[float, bool]:
        if geometry is None or geometry.is_empty:
            return 0.0, False
        if geometry.geom_type in {"Point", "MultiPoint", "LineString", "MultiLineString"}:
            exposed = geometry.intersects(flood_geometry)
            return float(exposed), exposed
        area = geometry.area
        overlap = geometry.intersection(flood_geometry).area
        fraction = float(overlap / area) if area else 0.0
        return fraction, geometry.centroid.within(flood_geometry) or fraction >= overlap_fraction

    measured = result.geometry.apply(measurements)
    result["overlap_fraction"] = measured.apply(lambda item: item[0])
    result["exposed"] = measured.apply(lambda item: item[1])
    return result[result["exposed"]].copy()


def _read_osm(path: Path, crs: str) -> gpd.GeoDataFrame:
    try:
        data = gpd.read_parquet(path)
    except Exception as exc:
        raise ImpactAnalysisError(f"Unable to read cached OSM data {path}: {exc}") from exc
    if data.empty:
        return gpd.GeoDataFrame(data, geometry="geometry", crs="EPSG:4326").to_crs(crs)
    return data.to_crs(crs)


def _road_geometry(graph: nx.MultiGraph, u: Any, v: Any, data: dict[str, Any]) -> Any:
    geometry = data.get("geometry")
    if geometry is not None:
        return geometry
    return LineString(
        [
            (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"])),
            (float(graph.nodes[v]["x"]), float(graph.nodes[v]["y"])),
        ]
    )


def potential_isolation(
    graph: nx.MultiDiGraph,
    settlements: gpd.GeoDataFrame,
    flood_geometry: Any,
    outer_boundary: Any,
    exit_tolerance_m: float,
    minimum_overlap_m: float,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise ImpactAnalysisError("Network analysis requires osmnx") from exc

    if settlements.empty:
        raise ImpactAnalysisError(
            "No named OSM settlements were available; isolation cannot be reported as zero"
        )
    projected_graph = graph
    graph_crs = projected_graph.graph.get("crs")
    if graph_crs is None:
        raise ImpactAnalysisError("OSM drive graph has no CRS")
    undirected = nx.MultiGraph(projected_graph.to_undirected())
    exits = {
        node
        for node, data in undirected.nodes(data=True)
        if outer_boundary.boundary.distance(Point(float(data["x"]), float(data["y"])))
        <= exit_tolerance_m
    }
    if not exits:
        raise ImpactAnalysisError("No road-network exits were found near the buffered AOI boundary")

    disrupted = undirected.copy()
    removed_edges = 0
    for u, v, key, data in list(disrupted.edges(keys=True, data=True)):
        geometry = _road_geometry(disrupted, u, v, data)
        if geometry.intersection(flood_geometry).length >= minimum_overlap_m:
            disrupted.remove_edge(u, v, key)
            removed_edges += 1

    records: list[dict[str, Any]] = []
    evaluated = 0
    unique = settlements.copy()
    if "name" not in unique.columns:
        raise ImpactAnalysisError("OSM settlement features do not contain names")
    unique = unique[unique["name"].notna()].drop_duplicates(subset=["name"])
    if unique.empty:
        raise ImpactAnalysisError("OSM settlement features contain no named places")
    points = unique.geometry.apply(
        lambda geometry: (
            geometry if geometry.geom_type == "Point" else geometry.representative_point()
        )
    )
    nearest = ox.distance.nearest_nodes(
        projected_graph,
        X=points.x.to_numpy(),
        Y=points.y.to_numpy(),
    )
    for (_, settlement), point, node in zip(unique.iterrows(), points, nearest, strict=True):
        if node not in undirected:
            continue
        baseline_component = nx.node_connected_component(undirected, node)
        baseline_reachable = bool(baseline_component & exits)
        if not baseline_reachable:
            continue
        evaluated += 1
        after_component = (
            nx.node_connected_component(disrupted, node) if node in disrupted else set()
        )
        potentially_isolated = not bool(after_component & exits)
        if potentially_isolated:
            records.append(
                {
                    "source_id": f"osm-place-{_safe_text(settlement.get('osmid')) or settlement.name}",
                    "name": _safe_text(settlement.get("name")),
                    "place": _safe_text(settlement.get("place")),
                    "status": "potentially isolated",
                    "assumption": "Road edges with at least 15 m detected-flood overlap are unavailable",
                    "geometry": point,
                }
            )
    if evaluated == 0:
        raise ImpactAnalysisError(
            "No named settlement was reachable from an AOI exit in the baseline graph"
        )
    if records:
        isolated = gpd.GeoDataFrame(records, geometry="geometry", crs=settlements.crs)
    else:
        isolated = gpd.GeoDataFrame(
            {"source_id": [], "name": [], "place": [], "status": [], "assumption": []},
            geometry=gpd.GeoSeries([], crs=settlements.crs),
        )
    return isolated, {
        "settlements_evaluated": evaluated,
        "potentially_isolated_count": len(records),
        "potentially_isolated_names": [record["name"] for record in records],
        "hypothetically_removed_road_edges": removed_edges,
        "method": "Baseline versus post-removal reachability to buffered-AOI boundary exits",
        "caveat": "A screening signal based on detected overlap, not confirmed road closure.",
    }


def _write_web_layer(path: Path, data: gpd.GeoDataFrame, fields: list[str]) -> Path:
    if data.empty:
        return write_feature_collection(path, [])
    selected = [field for field in fields if field in data.columns] + ["geometry"]
    web = data[selected].copy()
    web["geometry"] = web.geometry.simplify(1.0, preserve_topology=True)
    web = web.to_crs("EPSG:4326")
    for column in web.columns:
        if column != "geometry":
            web[column] = (
                web[column].apply(_safe_text) if web[column].dtype == object else web[column]
            )
    web.to_file(path, driver="GeoJSON")
    return path


def impact(config: PipelineConfig) -> list[Path]:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise ImpactAnalysisError("Impact analysis requires osmnx") from exc

    flood = gpd.read_file(
        config.path("processed") / "detection" / "flood_extent.gpkg",
        layer="flood_extent",
    ).to_crs(config.event.analysis_crs)
    flood_geometry = unary_union(flood.geometry)
    if flood_geometry.is_empty:
        raise ImpactAnalysisError("Flood evidence geometry is empty")

    osm_dir = config.path("raw") / "osm"
    roads = _read_osm(osm_dir / "roads.parquet", config.event.analysis_crs)
    railways = _read_osm(osm_dir / "railways.parquet", config.event.analysis_crs)
    buildings = _read_osm(osm_dir / "buildings.parquet", config.event.analysis_crs)
    assets = _read_osm(osm_dir / "critical_assets.parquet", config.event.analysis_crs)
    settlements = _read_osm(osm_dir / "settlements.parquet", config.event.analysis_crs)
    graph = ox.load_graphml(osm_dir / "drive.graphml")
    graph = ox.project_graph(graph, to_crs=config.event.analysis_crs)

    exposed_roads = line_exposure(roads, flood_geometry, config.impact.min_linear_overlap_m)
    exposed_railways = line_exposure(railways, flood_geometry, config.impact.min_linear_overlap_m)
    exposed_buildings = polygon_exposure(
        buildings, flood_geometry, config.impact.building_overlap_fraction
    )
    exposed_assets = polygon_exposure(
        assets, flood_geometry, config.impact.building_overlap_fraction
    )

    west, south, east, north = config.event.bounds_wgs84
    outer = (
        gpd.GeoSeries([box(west, south, east, north)], crs="EPSG:4326")
        .to_crs(config.event.analysis_crs)[0]
        .buffer(config.impact.osm_buffer_m)
    )
    isolated, isolation_summary = potential_isolation(
        graph,
        settlements,
        flood_geometry,
        outer,
        config.impact.boundary_exit_tolerance_m,
        config.impact.min_linear_overlap_m,
    )

    if "highway" in exposed_roads.columns:
        exposed_roads["road_class"] = exposed_roads["highway"].apply(_normalized_road_class)
        by_class = exposed_roads.groupby("road_class")["overlap_m"].agg(["count", "sum"])
        road_classes = {
            str(index): {"segments": int(row["count"]), "overlap_km": float(row["sum"] / 1000)}
            for index, row in by_class.iterrows()
        }
    else:
        road_classes = {}
    asset_categories = (
        exposed_assets["amenity"].apply(_safe_text).value_counts().to_dict()
        if "amenity" in exposed_assets.columns
        else {}
    )
    infrastructure = {
        "detected_flood_area_km2": float(flood_geometry.area / 1_000_000),
        "exposed_road_segments": len(exposed_roads),
        "exposed_road_overlap_km": float(exposed_roads.get("overlap_m", []).sum() / 1000)
        if not exposed_roads.empty
        else 0.0,
        "roads_by_class": road_classes,
        "exposed_railway_segments": len(exposed_railways),
        "exposed_railway_overlap_km": float(exposed_railways.get("overlap_m", []).sum() / 1000)
        if not exposed_railways.empty
        else 0.0,
        "exposed_buildings": len(exposed_buildings),
        "exposed_critical_assets": len(exposed_assets),
        "critical_assets_by_type": {
            str(key): int(value) for key, value in asset_categories.items()
        },
        "terminology": "Intersection/exposure screening; not confirmed damage or closure.",
    }

    output_dir = config.path("processed") / "impact"
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = [
        _write_web_layer(
            output_dir / "exposed_roads.geojson",
            exposed_roads,
            ["osmid", "name", "road_class", "overlap_m"],
        ),
        _write_web_layer(
            output_dir / "exposed_railways.geojson",
            exposed_railways,
            ["osmid", "name", "railway", "overlap_m"],
        ),
        _write_web_layer(
            output_dir / "exposed_buildings.geojson",
            exposed_buildings,
            ["osmid", "name", "building", "overlap_fraction"],
        ),
        _write_web_layer(
            output_dir / "exposed_critical_assets.geojson",
            exposed_assets,
            ["osmid", "name", "amenity", "overlap_fraction"],
        ),
        _write_web_layer(
            output_dir / "potentially_isolated_settlements.geojson",
            isolated,
            ["source_id", "name", "place", "status", "assumption"],
        ),
    ]
    summary_path = atomic_write_json(
        output_dir / "impact.json",
        {
            "event_id": config.event.id,
            "generated_at": utc_now(),
            "infrastructure": infrastructure,
            "isolation": isolation_summary,
        },
    )
    outputs.append(summary_path)
    return outputs
