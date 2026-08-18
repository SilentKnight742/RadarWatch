# Data contracts

## Configuration

`configs/valencia.yaml` is validated with Pydantic and rejects unknown fields.
It owns the AOI, CRS, exact SAR products, thresholds, morphology, CEMS sources,
OSM rules, and workspace paths. Its canonical JSON representation is hashed for
stage invalidation; the machine-specific resolved workspace root is excluded.

## Stage record

Each `data/stages/<stage>.json` contains:

- `schema_version`, `stage`, `status`, and `generated_at`;
- elapsed `runtime_seconds`;
- `config_sha256`;
- the RadarWatch software version and preceding stage-record hash;
- explicit input paths, byte sizes, and SHA-256 hashes; and
- output paths, byte sizes, and SHA-256 hashes.

A failed stage records its exception type and message. A completed stage is
reused only when configuration, dependency, output existence, and every output
hash still match.

## Public metrics

`metrics.json` contains:

- event identity, bounds, CRS, and acquisition times;
- water threshold, evidence areas, feature count, and sensitivity;
- infrastructure exposure grouped by type/class;
- evaluated and potentially isolated settlements;
- both evaluation blocks; and
- measured stage runtimes.

Evaluation fields are constrained to valid ranges by `MetricsContract`.

## Public provenance

`provenance.json` lists source provider, product/reference identity, acquisition
time, processing version, polarization, source hashes, CRS transformations,
resolution, filtering, detection method, configuration hash, and generation
time. Credentials and absolute local paths are forbidden.

## Vector semantics

Published GeoJSON uses `EPSG:4326`. Measurements remain in projected units:

- flood polygons: `feature_id`, `evidence`, `high_fraction`, `area_km2`,
  `source`;
- linear exposure: source identifiers/tags and `overlap_m`;
- building/facility exposure: source identifiers/tags and `overlap_fraction`;
- isolation: stable OSM-derived `source_id`, name, OSM place type,
  `potentially isolated` status, and the counterfactual assumption.
