import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point, Polygon, box

from radarwatch.impact import (
    _normalized_road_class,
    line_exposure,
    polygon_exposure,
    potential_isolation,
)


def test_linear_and_polygon_exposure_rules() -> None:
    flood = box(4, -1, 8, 4)
    roads = gpd.GeoDataFrame(
        {"name": ["long", "touch"]},
        geometry=[LineString([(0, 0), (10, 0)]), LineString([(0, 5), (10, 5)])],
        crs="EPSG:32630",
    )
    buildings = gpd.GeoDataFrame(
        {"name": ["exposed", "clear"]},
        geometry=[box(6, 1, 9, 3), box(20, 20, 22, 22)],
        crs="EPSG:32630",
    )

    exposed_roads = line_exposure(roads, flood, minimum_overlap_m=3)
    exposed_buildings = polygon_exposure(buildings, flood, overlap_fraction=0.1)

    assert exposed_roads["name"].tolist() == ["long"]
    assert exposed_roads.iloc[0]["overlap_m"] == 4
    assert exposed_buildings["name"].tolist() == ["exposed"]


def test_nonstandard_road_lifecycle_tag_is_not_published_as_damage() -> None:
    assert _normalized_road_class("motorway") == "motorway"
    assert _normalized_road_class("destroyed") == "other"


def make_graph(with_second_exit: bool = False) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(crs="EPSG:32630")
    graph.add_node(0, x=0.0, y=50.0)
    graph.add_node(1, x=40.0, y=50.0)
    graph.add_node(2, x=80.0, y=50.0)
    graph.add_edge(0, 1, geometry=LineString([(0, 50), (40, 50)]), length=40)
    graph.add_edge(1, 0, geometry=LineString([(40, 50), (0, 50)]), length=40)
    graph.add_edge(1, 2, geometry=LineString([(40, 50), (80, 50)]), length=40)
    graph.add_edge(2, 1, geometry=LineString([(80, 50), (40, 50)]), length=40)
    if with_second_exit:
        graph.add_node(3, x=100.0, y=50.0)
        graph.add_edge(2, 3, geometry=LineString([(80, 50), (100, 50)]), length=20)
        graph.add_edge(3, 2, geometry=LineString([(100, 50), (80, 50)]), length=20)
    return graph


def make_settlements() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["Testville"], "place": ["town"]},
        geometry=[Point(80, 50)],
        crs="EPSG:32630",
    )


def test_potential_isolation_when_only_route_is_intersected() -> None:
    isolated, summary = potential_isolation(
        make_graph(),
        make_settlements(),
        box(55, 45, 70, 55),
        Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        exit_tolerance_m=1,
        minimum_overlap_m=5,
    )

    assert summary["settlements_evaluated"] == 1
    assert summary["potentially_isolated_count"] == 1
    assert isolated.iloc[0]["name"] == "Testville"


def test_second_exit_prevents_isolation() -> None:
    isolated, summary = potential_isolation(
        make_graph(with_second_exit=True),
        make_settlements(),
        box(55, 45, 70, 55),
        Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        exit_tolerance_m=1,
        minimum_overlap_m=5,
    )

    assert summary["settlements_evaluated"] == 1
    assert summary["potentially_isolated_count"] == 0
    assert isolated.empty
