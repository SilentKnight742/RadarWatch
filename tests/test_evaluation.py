import numpy as np

from radarwatch.evaluation import binary_metrics


def test_binary_metrics_are_pixel_area_aware() -> None:
    predicted = np.array([[1, 1], [0, 0]], dtype=bool)
    reference = np.array([[1, 0], [1, 0]], dtype=bool)
    valid = np.ones((2, 2), dtype=bool)

    result = binary_metrics(
        predicted,
        reference,
        valid,
        0.0009,
        label="test",
        caveat="not ground truth",
        acquired_at="2024-10-31T18:02:00Z",
    )

    assert result.iou == 1 / 3
    assert result.dice_f1 == 0.5
    assert result.precision == 0.5
    assert result.recall == 0.5
    assert result.false_positive_rate == 0.5
    assert result.detected_area_km2 == 0.0018
    assert result.reference_area_km2 == 0.0018
    assert result.overlap_area_km2 == 0.0009
