import numpy as np
import rasterio

from radarwatch.raster import linear_to_db, masked_median, target_grid, write_raster


def test_linear_to_db_preserves_invalid_as_nan() -> None:
    values = np.array([[1.0, 0.1, 0.0, -1.0, np.nan]], dtype=np.float32)

    result = linear_to_db(values)

    np.testing.assert_allclose(result[0, :2], [0.0, -10.0], atol=1e-5)
    assert np.isnan(result[0, 2:]).all()


def test_masked_median_does_not_fill_large_nodata_region() -> None:
    values = np.full((5, 5), np.nan, dtype=np.float32)
    values[1:4, 1:4] = np.arange(9, dtype=np.float32).reshape(3, 3)

    result = masked_median(values, 3)

    assert result[2, 2] == 4
    assert np.isnan(result[0, 0])


def test_target_grid_is_30_m_aligned() -> None:
    transform, width, height = target_grid((-0.58, 39.36, -0.39, 39.50), "EPSG:32630", 30)

    assert transform.a == 30
    assert transform.e == -30
    assert transform.c % 30 == 0
    assert transform.f % 30 == 0
    assert width > 500
    assert height > 500


def test_written_raster_is_a_cloud_optimized_geotiff(tmp_path) -> None:
    path = write_raster(
        tmp_path / "fixture.tif",
        np.ones((32, 32), dtype=np.float32),
        rasterio.transform.from_origin(720000, 4360000, 30, 30),
        "EPSG:32630",
        nodata=-9999.0,
        dtype="float32",
    )

    with rasterio.open(path) as dataset:
        assert dataset.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
