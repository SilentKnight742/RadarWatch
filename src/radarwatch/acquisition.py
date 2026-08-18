"""External data discovery and acquisition."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import geopandas as gpd
import rasterio
import requests
from shapely.geometry import box

from radarwatch.config import PipelineConfig, ReferenceConfig
from radarwatch.exceptions import AcquisitionError, DataValidationError
from radarwatch.utils import atomic_write_json, file_record, utc_now

ASF_SEARCH_URL = "https://api.daac.asf.alaska.edu/services/search/param"
ASF_DATA_URL = "https://datapool.asf.alaska.edu/RTC/OPERA-S1"
PRODUCT_ID_PATTERN = re.compile(
    r"^OPERA_L2_RTC-S1_(T\d{3})-(\d+)-(IW\d)_"
    r"(\d{8}T\d{6}Z)_\d{8}T\d{6}Z_S1[AB]_30_v([\d.]+)$"
)


def _request_json(
    url: str, *, params: dict[str, Any] | None = None, timeout: int = 60
) -> dict[str, Any]:
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise AcquisitionError(f"Unable to read {url}: {exc}") from exc


def search_opera_product(product_id: str) -> dict[str, Any]:
    payload = _request_json(
        ASF_SEARCH_URL,
        params={
            "dataset": "OPERA-S1",
            "granule_list": product_id,
            "output": "geojson",
        },
    )
    features = payload.get("features", [])
    if len(features) != 1:
        raise AcquisitionError(
            f"Expected one ASF catalogue record for {product_id}, found {len(features)}"
        )
    return features[0]["properties"]


def validate_opera_record(record: dict[str, Any], product_id: str, config: PipelineConfig) -> None:
    product_match = PRODUCT_ID_PATTERN.fullmatch(product_id)
    if product_match is None:
        raise DataValidationError(f"Invalid configured OPERA product ID: {product_id}")
    track, burst_number, subswath, acquired_at, product_version = product_match.groups()
    if track != f"T{config.sar.relative_orbit:03d}":
        raise DataValidationError(
            f"Configured track in {product_id} does not match "
            f"sar.relative_orbit={config.sar.relative_orbit}"
        )
    expected = {
        "sceneName": product_id,
        "pathNumber": config.sar.relative_orbit,
        "flightDirection": config.sar.flight_direction,
        "productVersion": config.sar.product_version,
        "operaBurstID": f"{track}_{burst_number}_{subswath}",
        "startTime": (
            f"{acquired_at[:4]}-{acquired_at[4:6]}-{acquired_at[6:8]}"
            f"T{acquired_at[9:11]}:{acquired_at[11:13]}:{acquired_at[13:15]}Z"
        ),
    }
    if product_version != config.sar.product_version:
        raise DataValidationError(
            f"Configured product version in {product_id} does not match "
            f"sar.product_version={config.sar.product_version}"
        )
    mismatches = {
        key: {"expected": value, "actual": record.get(key)}
        for key, value in expected.items()
        if str(record.get(key)) != str(value)
    }
    if set(record.get("polarization", [])) != set(config.sar.polarizations):
        mismatches["polarization"] = {
            "expected": config.sar.polarizations,
            "actual": record.get("polarization"),
        }
    if mismatches:
        raise DataValidationError(
            f"ASF catalogue record does not match configuration for {product_id}: {mismatches}"
        )


def _earthdata_session() -> requests.Session:
    try:
        import earthaccess

        auth = earthaccess.login(strategy="netrc")
        if not getattr(auth, "authenticated", False):
            raise AcquisitionError("NASA Earthdata authentication was not accepted")
        return earthaccess.get_requests_https_session()
    except AcquisitionError:
        raise
    except Exception as exc:
        raise AcquisitionError(
            "NASA Earthdata authorization is required for OPERA downloads. "
            "Create ~/.netrc (or %USERPROFILE%/_netrc on Windows) for "
            "urs.earthdata.nasa.gov and accept the ASF terms in Vertex."
        ) from exc


def _stream_download(
    session: requests.Session, url: str, target: Path, expected_bytes: int | None
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    try:
        with session.get(url, stream=True, timeout=(30, 300)) as response:
            if response.status_code in {401, 403}:
                raise AcquisitionError(
                    "NASA Earthdata authorization is required for OPERA downloads. "
                    "Create ~/.netrc (or %USERPROFILE%/_netrc on Windows) for "
                    "urs.earthdata.nasa.gov and accept the ASF terms in Vertex."
                )
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
        if expected_bytes is not None and size != expected_bytes:
            raise AcquisitionError(
                f"Download size mismatch for {target.name}: expected {expected_bytes}, got {size}"
            )
        temporary.replace(target)
    except AcquisitionError:
        temporary.unlink(missing_ok=True)
        raise
    except requests.RequestException as exc:
        temporary.unlink(missing_ok=True)
        raise AcquisitionError(f"Failed to download {url}: {exc}") from exc


def _asset_url(record: dict[str, Any], filename: str) -> str:
    urls = [record.get("url"), *record.get("additionalUrls", [])]
    return next(
        (url for url in urls if isinstance(url, str) and url.endswith(filename)),
        f"{ASF_DATA_URL}/{filename}",
    )


def _raster_asset_record(
    path: Path,
    workspace: Path,
    *,
    url: str,
    asset_type: str,
) -> dict[str, Any]:
    record = file_record(path, workspace)
    try:
        with rasterio.open(path) as dataset:
            record.update(
                {
                    "url": url,
                    "asset_type": asset_type,
                    "crs": str(dataset.crs),
                    "shape": [dataset.height, dataset.width],
                    "transform": list(dataset.transform)[:6],
                }
            )
    except rasterio.RasterioError as exc:
        raise DataValidationError(f"Downloaded asset is not a readable GeoTIFF: {path}") from exc
    return record


def acquire_sar(
    config: PipelineConfig, offline: bool = False
) -> tuple[list[Path], list[dict[str, Any]]]:
    raw_dir = config.path("raw") / "sar"
    raw_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    sources: list[dict[str, Any]] = []
    session: requests.Session | None = None
    catalogue: list[tuple[str, str, dict[str, Any], Path]] = []

    # Resolve and validate the complete matched set before downloading any bytes.
    for role, group in (("before", config.sar.before), ("after", config.sar.after)):
        for product_id in group.products:
            if offline:
                record_path = raw_dir / f"{product_id}.catalog.json"
                if not record_path.exists():
                    raise AcquisitionError(f"Offline catalogue record is missing: {record_path}")
                record = json.loads(record_path.read_text(encoding="utf-8"))
            else:
                record = search_opera_product(product_id)
                record_path = atomic_write_json(raw_dir / f"{product_id}.catalog.json", record)
            validate_opera_record(record, product_id, config)
            outputs.append(record_path)
            catalogue.append((role, product_id, record, record_path))

    for role, product_id, record, record_path in catalogue:
        byte_table = record.get("bytes", {})
        assets: list[dict[str, Any]] = []
        for suffix in ("VV.tif", "VH.tif", "mask.tif"):
            filename = f"{product_id}_{suffix}"
            target = raw_dir / filename
            url = _asset_url(record, filename)
            expected_bytes = byte_table.get(filename, {}).get("bytes")
            if not target.exists():
                if offline:
                    raise AcquisitionError(f"Offline SAR asset is missing: {target}")
                if session is None:
                    session = _earthdata_session()
                _stream_download(
                    session,
                    url,
                    target,
                    expected_bytes,
                )
            elif expected_bytes is not None and target.stat().st_size != expected_bytes:
                raise DataValidationError(
                    f"Cached SAR asset has wrong size: {target} (delete it and reacquire)"
                )
            outputs.append(target)
            assets.append(
                _raster_asset_record(
                    target,
                    config.workspace,
                    url=url,
                    asset_type=suffix.removesuffix(".tif"),
                )
            )
        sources.append(
            {
                "kind": "sar",
                "role": role,
                "provider": "NASA ASF DAAC / OPERA",
                "product_id": product_id,
                "acquired_at": record.get("startTime"),
                "relative_orbit": record.get("pathNumber"),
                "flight_direction": record.get("flightDirection"),
                "product_version": record.get("productVersion"),
                "polarizations": record.get("polarization"),
                "license": "NASA Earth Science Data and Information Policy",
                "attribution": "NASA/JPL OPERA, distributed by NASA ASF DAAC",
                "catalogue_record": file_record(record_path, config.workspace),
                "assets": assets,
            }
        )
    return outputs, sources


def acquire_reference(
    reference: ReferenceConfig,
    name: str,
    target_dir: Path,
    offline: bool = False,
    workspace: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    target = target_dir / f"cems_{name}.geojson"
    if not target.exists():
        if offline:
            raise AcquisitionError(f"Offline reference is missing: {target}")
        payload = _request_json(
            f"{reference.url}/query",
            params={
                "where": "notation='Flooded area'",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": "5000",
            },
        )
        features = payload.get("features", [])
        if not features:
            raise AcquisitionError(f"Copernicus reference '{name}' returned no flooded areas")
        atomic_write_json(target, payload)
    return target, {
        "kind": "reference_flood_extent",
        "name": name,
        "provider": "Copernicus Emergency Management Service EMSR773",
        "url": reference.url,
        "license": "Copernicus Emergency Management Service data policy",
        "attribution": "European Union, Copernicus EMSR773",
        "acquired_at": reference.acquired_at.isoformat(),
        "label": reference.label,
        "caveat": reference.caveat,
        "asset": file_record(target, workspace),
    }


def _write_gdf(path: Path, data: gpd.GeoDataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = data.copy()
    for column in serializable.columns:
        if column != "geometry" and serializable[column].dtype == object:
            serializable[column] = serializable[column].apply(
                lambda value: (
                    ", ".join(str(item) for item in value)
                    if isinstance(value, (list, tuple, set))
                    else (None if value is None else str(value))
                )
            )
    try:
        serializable.to_parquet(path, index=False, compression="zstd")
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise AcquisitionError(f"Unable to cache OSM layer {path.name}: {exc}") from exc


def _select_columns(data: gpd.GeoDataFrame, names: tuple[str, ...]) -> gpd.GeoDataFrame:
    selected = [name for name in names if name in data.columns]
    return data[[*selected, "geometry"]].copy()


def acquire_osm(config: PipelineConfig, offline: bool = False) -> tuple[list[Path], dict[str, Any]]:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise AcquisitionError("OSM acquisition requires the 'osmnx' dependency") from exc

    osm_dir = config.path("raw") / "osm"
    osm_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(osm_dir / "overpass-cache")
    names = ("roads", "railways", "buildings", "critical_assets", "settlements")
    outputs = [osm_dir / f"{name}.parquet" for name in names]
    graph_path = osm_dir / "drive.graphml"
    outputs.append(graph_path)
    if all(path.exists() for path in outputs):
        return outputs, {
            "kind": "infrastructure",
            "provider": "OpenStreetMap contributors",
            "license": "ODbL",
            "attribution": "© OpenStreetMap contributors",
            "assets": [file_record(path, config.workspace) for path in outputs],
        }
    if offline:
        missing = [str(path) for path in outputs if not path.exists()]
        raise AcquisitionError(f"Offline OSM cache is incomplete: {missing}")

    west, south, east, north = config.event.bounds_wgs84
    aoi = gpd.GeoSeries([box(west, south, east, north)], crs="EPSG:4326")
    expanded = (
        aoi.to_crs(config.event.analysis_crs).buffer(config.impact.osm_buffer_m).to_crs("EPSG:4326")
    )
    bounds = tuple(expanded.total_bounds.tolist())
    tags = {
        "building": True,
        "railway": True,
        "amenity": config.impact.critical_amenities,
        "place": config.impact.place_tags,
    }
    try:
        features = ox.features_from_bbox(bounds, tags=tags)
        graph = ox.graph_from_bbox(bounds, network_type="drive", simplify=True)
    except Exception as exc:
        raise AcquisitionError(f"OpenStreetMap/Overpass acquisition failed: {exc}") from exc

    if features.empty:
        raise AcquisitionError("OpenStreetMap returned no features for the buffered AOI")
    features = features.reset_index()
    roads = _select_columns(
        ox.graph_to_gdfs(graph, nodes=False, edges=True).reset_index(),
        ("u", "v", "key", "osmid", "name", "highway", "length"),
    )
    railways = _select_columns(
        features[features["railway"].notna()],
        ("osmid", "name", "railway"),
    )
    buildings = _select_columns(
        features[features["building"].notna()],
        ("osmid", "name", "building"),
    )
    assets = _select_columns(
        features[features["amenity"].isin(config.impact.critical_amenities)],
        ("osmid", "name", "amenity"),
    )
    settlements = _select_columns(
        features[features["place"].isin(config.impact.place_tags)],
        ("osmid", "name", "place"),
    )
    for frame in (roads, railways, buildings, assets, settlements):
        frame.set_crs("EPSG:4326", allow_override=True, inplace=True)
    for path, frame in zip(
        outputs[:-1], (roads, railways, buildings, assets, settlements), strict=True
    ):
        _write_gdf(path, frame)
    ox.save_graphml(graph, graph_path)
    return outputs, {
        "kind": "infrastructure",
        "provider": "OpenStreetMap contributors",
        "license": "ODbL",
        "attribution": "© OpenStreetMap contributors",
        "query_bounds_wgs84": list(bounds),
        "assets": [file_record(path, config.workspace) for path in outputs],
    }


def acquire(config: PipelineConfig, offline: bool = False) -> list[Path]:
    config.ensure_directories()
    outputs: list[Path] = []
    sources: list[dict[str, Any]] = []

    reference_dir = config.path("raw") / "references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    for name, reference in (
        ("exact_time", config.references.exact_time),
        ("optical", config.references.optical),
    ):
        path, source = acquire_reference(
            reference, name, reference_dir, offline, workspace=config.workspace
        )
        outputs.append(path)
        sources.append(source)

    osm_outputs, osm_source = acquire_osm(config, offline)
    outputs.extend(osm_outputs)
    sources.append(osm_source)

    sar_outputs, sar_sources = acquire_sar(config, offline)
    outputs.extend(sar_outputs)
    sources.extend(sar_sources)

    manifest = atomic_write_json(
        config.path("raw") / "source_manifest.json",
        {
            "event_id": config.event.id,
            "generated_at": utc_now(),
            "sources": sources,
        },
    )
    outputs.append(manifest)
    return outputs
