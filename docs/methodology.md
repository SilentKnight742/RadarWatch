# Methodology

## Study design

RadarWatch V1 studies Copernicus EMSR773 AOI03 in Horta Sud, Valencia. It uses
two ascending-track 103 OPERA burst IDs on 19 October and the same burst IDs on
31 October 2024. Matching orbit and burst geometry reduces false change caused
by viewing-direction differences.

All analysis uses WGS 84 / UTM zone 30N (`EPSG:32630`) at 30 m. Web products
are reprojected to `EPSG:4326` only after measurements are complete.

## SAR preparation

OPERA RTC-S1 provides terrain-corrected, geocoded Sentinel-1 power products.
For each date and polarization RadarWatch:

1. validates product ID, acquisition time, orbit, direction, version, and
   polarization against the ASF catalogue;
2. reprojects the two bursts to an explicitly aligned target grid;
3. retains mask class 0 and excludes invalid fill plus layover/shadow-affected
   classes from flood inference;
4. averages valid burst overlap;
5. converts power to dB with `10 log10(power)`; and
6. applies a nodata-preserving 3x3 median filter.

The stage fails when less than 95% of the AOI has common valid coverage across
before/after VV/VH.

## Evidence classification

The low-backscatter threshold is the first boundary returned by three-class
Multi-Otsu segmentation of post-event VV. It is constrained to `[-20, -12] dB`
to prevent pathological thresholds in a land-dominated AOI.

Let:

- `ΔVV = afterVV - beforeVV`
- `ΔVH = afterVH - beforeVH`

For the default scenario:

- moderate evidence requires low post-event VV and either `ΔVV <= -3 dB` or
  `ΔVH <= -2 dB`;
- high evidence requires low post-event VV and both decrease rules.

A 3x3 closing fills small internal gaps. Holes and connected components smaller
than 10 pixels are removed. This is a change-evidence heuristic, not a semantic
segmentation model.

Strict `(-4,-3)` and lenient `(-2,-1)` VV/VH variants quantify threshold
sensitivity. The references do not tune the default thresholds.

## Evaluation

Both reference products are rasterized onto the prediction grid. Metrics use
only pixels with common valid SAR coverage.

- **Operational-product agreement:** the CEMS AOI01 extent uses the same
  Sentinel-1 acquisition and therefore is not independent validation.
- **Cross-sensor temporal comparison:** the AOI03 extent uses higher-resolution
  optical interpretation but was acquired about eight hours earlier.

Reported quantities are IoU, Dice/F1, precision, recall, false-positive rate,
overlap area, predicted/reference area, area difference, and valid comparison
area. Neither source is labelled field-validated ground truth.

## Consequence analysis

Measurements use projected geometries:

- a road or railway is intersected when at least 15 m of its centreline overlaps
  detected evidence;
- a building is exposed when its centroid is inside the extent or at least 10%
  of its footprint overlaps;
- critical facilities use the same rule for hospitals, clinics, ambulance
  stations, fire stations, and police facilities.

Potential isolation is a counterfactual screening calculation. Named OSM
settlements are snapped to a drivable graph extending 2 km beyond the AOI. Road
edges with at least 15 m of overlap are hypothetically removed. A settlement is
flagged only if it could reach a boundary exit before removal and no longer can
after removal.

This does not confirm a closure, damage, or real-world isolation.
