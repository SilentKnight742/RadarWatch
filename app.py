"""Public RadarWatch Streamlit case study.

This runtime intentionally consumes only precomputed PNG, GeoJSON, and JSON
artifacts. It never performs SAR processing or external data acquisition.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import folium
import pandas as pd
import streamlit as st
from branca.element import Element
from folium.plugins import Fullscreen, SideBySideLayers

ROOT = Path(__file__).resolve().parent
DEMO = Path(os.environ.get("RADARWATCH_DEMO_DIR", ROOT / "demo_data" / "valencia"))


def read_json(name: str) -> dict[str, Any]:
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


def read_layer(name: str) -> dict[str, Any]:
    path = DEMO / name
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {
            "type": "FeatureCollection",
            "features": [],
        }
    )


def bundle_integrity_errors() -> list[str]:
    try:
        manifest = read_json("manifest.json")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest.json is unreadable: {exc}"]
    errors: list[str] = []
    demo_root = DEMO.resolve()
    for asset in manifest.get("assets", []):
        relative = asset.get("path")
        if not isinstance(relative, str):
            errors.append("manifest contains an invalid asset path")
            continue
        path = (DEMO / relative).resolve()
        if path != demo_root and demo_root not in path.parents:
            errors.append(f"manifest path leaves the demo directory: {relative}")
            continue
        if not path.exists():
            errors.append(f"declared asset is missing: {relative}")
            continue
        if path.stat().st_size != asset.get("bytes"):
            errors.append(f"asset size does not match manifest: {relative}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != asset.get("sha256"):
            errors.append(f"asset hash does not match manifest: {relative}")
    before_path = DEMO / "before_vv.png"
    after_path = DEMO / "after_vv.png"
    if (
        before_path.exists()
        and after_path.exists()
        and before_path.read_bytes() == after_path.read_bytes()
    ):
        errors.append("before and after SAR browse images are identical")
    return errors


def add_geojson(
    map_object: folium.Map,
    filename: str,
    name: str,
    color: str,
    *,
    fill_opacity: float = 0.3,
    weight: float = 2,
    show: bool = True,
    style_function: Any | None = None,
    feature_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> None:
    data = read_layer(filename)
    if feature_filter is not None:
        data = {
            **data,
            "features": [
                feature for feature in data.get("features", []) if feature_filter(feature)
            ],
        }
    if not data.get("features"):
        return

    def default_style(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "color": color,
            "weight": weight,
            "fillColor": color,
            "fillOpacity": fill_opacity,
        }

    tooltip_fields = [
        field
        for field in (
            "name",
            "evidence",
            "road_class",
            "amenity",
            "overlap_m",
            "status",
        )
        if any(field in feature.get("properties", {}) for feature in data["features"])
    ]
    layer = folium.GeoJson(
        data=data,
        name=name,
        show=show,
        style_function=style_function or default_style,
    )
    if tooltip_fields:
        folium.GeoJsonTooltip(
            fields=tooltip_fields,
            aliases=[field.replace("_", " ").title() for field in tooltip_fields],
            sticky=False,
        ).add_to(layer)
    layer.add_to(map_object)


def build_map(metrics: dict[str, Any]) -> folium.Map:
    west, south, east, north = metrics["event"]["bounds_wgs84"]
    center = [(south + north) / 2, (west + east) / 2]
    map_object = folium.Map(
        location=center,
        zoom_start=11,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
    )
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr="© OpenStreetMap contributors © CARTO",
        name="Context basemap",
        control=True,
    ).add_to(map_object)
    browse_west, browse_south, browse_east, browse_north = metrics["event"].get(
        "browse_bounds_wgs84", metrics["event"]["bounds_wgs84"]
    )
    bounds = [[browse_south, browse_west], [browse_north, browse_east]]
    before = folium.raster_layers.ImageOverlay(
        image=str(DEMO / "before_vv.png"),
        bounds=bounds,
        name="Before SAR · 19 Oct",
        opacity=1.0,
        control=False,
    )
    after = folium.raster_layers.ImageOverlay(
        image=str(DEMO / "after_vv.png"),
        bounds=bounds,
        name="After SAR · 31 Oct",
        opacity=1.0,
        control=False,
    )
    before.add_to(map_object)
    after.add_to(map_object)
    SideBySideLayers(before, after).add_to(map_object)
    map_object.get_root().html.add_child(
        Element(
            """
            <style>
              .radarwatch-date-badge {
                position: fixed;
                top: 12px;
                z-index: 1000;
                padding: 7px 11px;
                border: 1px solid rgba(255,255,255,.72);
                border-radius: 6px;
                box-shadow: 0 2px 8px rgba(15,23,42,.28);
                color: white;
                font: 700 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                letter-spacing: .04em;
                pointer-events: none;
              }
              .radarwatch-before {
                right: calc(50% + 28px);
                background: rgba(3,105,161,.92);
              }
              .radarwatch-after {
                left: calc(50% + 28px);
                background: rgba(190,24,93,.92);
              }
            </style>
            <div class="radarwatch-date-badge radarwatch-before">BEFORE · 19 OCT 2024</div>
            <div class="radarwatch-date-badge radarwatch-after">AFTER · 31 OCT 2024</div>
            """
        )
    )

    add_geojson(
        map_object,
        "flood_extent.geojson",
        "Moderate flood/change evidence",
        "#ff9f1c",
        fill_opacity=0.38,
        feature_filter=lambda feature: feature.get("properties", {}).get("evidence") == "moderate",
    )
    add_geojson(
        map_object,
        "flood_extent.geojson",
        "High flood/change evidence",
        "#ff365e",
        fill_opacity=0.58,
        feature_filter=lambda feature: feature.get("properties", {}).get("evidence") == "high",
    )
    add_geojson(
        map_object,
        "reference_exact_time.geojson",
        "CEMS exact-time operational extent",
        "#00d4ff",
        fill_opacity=0.08,
        show=False,
    )
    add_geojson(
        map_object,
        "reference_optical.geojson",
        "CEMS earlier optical extent",
        "#7b61ff",
        fill_opacity=0.08,
        show=False,
    )
    add_geojson(
        map_object,
        "exposed_roads.geojson",
        "Intersected roads",
        "#ffd166",
        fill_opacity=0,
        weight=3,
    )
    add_geojson(
        map_object,
        "exposed_railways.geojson",
        "Intersected railways",
        "#8338ec",
        fill_opacity=0,
        weight=3,
        show=False,
    )
    add_geojson(
        map_object,
        "exposed_buildings.geojson",
        "Exposed building footprints",
        "#9c6ade",
        fill_opacity=0.45,
        weight=1,
        show=False,
    )
    add_geojson(
        map_object,
        "exposed_critical_assets.geojson",
        "Exposed critical facilities",
        "#e63946",
        fill_opacity=0.65,
        weight=2,
    )
    add_geojson(
        map_object,
        "potentially_isolated_settlements.geojson",
        "Potentially isolated settlements",
        "#111827",
        fill_opacity=0.7,
        weight=3,
    )
    Fullscreen(position="topright").add_to(map_object)
    folium.LayerControl(collapsed=False).add_to(map_object)
    map_object.fit_bounds(bounds)
    return map_object


st.set_page_config(
    page_title="RadarWatch · Valencia Flood Intelligence",
    page_icon="📡",
    layout="wide",
)
st.markdown(
    """
    <style>
      .block-container {max-width: 1320px; padding-top: 4.75rem; padding-bottom: 2rem;}
      [data-testid="stMetric"] {background:#f6f8fb;border:1px solid #e5e7eb;padding:14px;border-radius:12px;}
      .eyebrow {letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:#d92d50;font-size:.78rem;}
      .caveat {border-left:4px solid #f59e0b;background:#fffbeb;padding:12px 16px;border-radius:4px;}
      .source-note {color:#5f6b7a;font-size:.88rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

required = [
    "manifest.json",
    "metrics.json",
    "provenance.json",
    "before_vv.png",
    "after_vv.png",
    "flood_extent.geojson",
]
missing = [name for name in required if not (DEMO / name).exists()]
if missing:
    st.error(
        "The precomputed Valencia demo bundle is not present. Run "
        "`radarwatch run --config configs/valencia.yaml` after configuring NASA Earthdata. "
        f"Missing: {', '.join(missing)}"
    )
    st.stop()

integrity_errors = bundle_integrity_errors()
if integrity_errors:
    st.error("The precomputed demo bundle is corrupt: " + "; ".join(integrity_errors))
    st.stop()

try:
    metrics = read_json("metrics.json")
    provenance = read_json("provenance.json")
except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
    st.error(f"The precomputed demo bundle cannot be read: {exc}")
    st.stop()
infrastructure = metrics["infrastructure"]
isolation = metrics["isolation"]

st.markdown(
    '<div class="eyebrow">SAR-based flood impact intelligence</div>', unsafe_allow_html=True
)
st.title("RadarWatch: Valencia 2024")
st.markdown(
    "A reproducible before/after Sentinel-1 analysis for Horta Sud that moves from "
    "surface-change evidence to infrastructure exposure and road-network consequence."
)
st.warning(
    "Interpretation: These are detected flood/change signals and intersections—not confirmed "
    "damage, destroyed assets, or verified road closures."
)

columns = st.columns(5)
columns[0].metric("Detected area", f"{infrastructure['detected_flood_area_km2']:.2f} km²")
columns[1].metric("Road overlap", f"{infrastructure['exposed_road_overlap_km']:.1f} km")
columns[2].metric("Exposed buildings", f"{infrastructure['exposed_buildings']:,}")
columns[3].metric("Critical facilities", f"{infrastructure['exposed_critical_assets']:,}")
columns[4].metric("Potentially isolated", isolation["potentially_isolated_count"])

st.subheader("Before, after, and consequence")
st.caption(
    "Drag the vertical divider: the left side is BEFORE (19 October) and the right side "
    "is AFTER (31 October). Both use the same fixed VV backscatter display scale. "
    "Use the layer control to inspect detected evidence, references, and exposed infrastructure."
)
st.iframe(build_map(metrics).get_root().render(), width="stretch", height=690, tab_index=0)
st.caption(
    "Data: NASA/JPL OPERA via ASF DAAC · European Union, Copernicus EMSR773 · "
    "© OpenStreetMap contributors (ODbL) · © CARTO basemap."
)

impact_tab, evaluation_tab, method_tab, provenance_tab = st.tabs(
    ["Impact", "Evaluation", "Method & limitations", "Provenance"]
)
with impact_tab:
    left, right = st.columns(2)
    with left:
        st.subheader("Infrastructure exposure")
        st.json(infrastructure, expanded=True)
    with right:
        st.subheader("Network screening")
        st.write(
            f"{isolation['settlements_evaluated']} named settlements were tested against "
            "road-network exits before and after hypothetical removal of intersected road edges."
        )
        if isolation["potentially_isolated_names"]:
            st.write("Potentially isolated: " + ", ".join(isolation["potentially_isolated_names"]))
        else:
            st.write(
                "No evaluated settlement lost all exit reachability under this screening rule."
            )
        st.caption(isolation["caveat"])

with evaluation_tab:
    rows = []
    for _key, values in metrics["evaluation"].items():
        rows.append(
            {
                "comparison": values["label"],
                "IoU": values["iou"],
                "Dice/F1": values["dice_f1"],
                "Precision": values["precision"],
                "Recall": values["recall"],
                "FPR": values["false_positive_rate"],
                "Reference area km²": values["reference_area_km2"],
            }
        )
        st.caption(f"{values['label']}: {values['caveat']}")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.subheader("Threshold sensitivity")
    st.dataframe(
        pd.DataFrame(metrics["detection"]["sensitivity"]).T.reset_index(names="scenario"),
        hide_index=True,
        width="stretch",
    )

with method_tab:
    st.markdown(
        """
        1. Mosaic matched ascending OPERA RTC-S1 bursts and align VV/VH to a 30 m grid.
        2. Convert power to dB and apply a 3x3 median filter.
        3. Identify low post-event VV with three-class Multi-Otsu segmentation.
        4. Combine low backscatter with VV/VH decreases; classify moderate and high evidence.
        5. Remove small components, vectorize, intersect OSM assets, and compare road reachability.

        **Limitations:** urban double-bounce can obscure water; wet soil and agriculture can resemble
        flooding; the flash flood evolved between acquisitions; OSM completeness varies; and the
        isolation result assumes intersected road edges are unavailable without observing closures.
        """
    )

with provenance_tab:
    st.json(provenance, expanded=False)
    st.markdown(
        '<p class="source-note">Sources: NASA ASF DAAC / OPERA RTC-S1; Copernicus Emergency '
        "Management Service EMSR773; © OpenStreetMap contributors (ODbL); CARTO basemap. "
        "RadarWatch is an independent portfolio project, not a NASA submission or affiliated product.</p>",
        unsafe_allow_html=True,
    )

download_columns = st.columns(3)
download_columns[0].download_button(
    "Download detected extent",
    data=(DEMO / "flood_extent.geojson").read_bytes(),
    file_name="radarwatch_valencia_flood_extent.geojson",
    mime="application/geo+json",
)
download_columns[1].download_button(
    "Download metrics",
    data=(DEMO / "metrics.json").read_bytes(),
    file_name="radarwatch_valencia_metrics.json",
    mime="application/json",
)
download_columns[2].download_button(
    "Download provenance",
    data=(DEMO / "provenance.json").read_bytes(),
    file_name="radarwatch_valencia_provenance.json",
    mime="application/json",
)
