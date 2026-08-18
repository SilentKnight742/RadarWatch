import numpy as np

from radarwatch.config import DetectionScenario
from radarwatch.detection import classify_evidence, derive_water_threshold


def test_classify_evidence_assigns_moderate_and_high() -> None:
    post = np.full((9, 9), -10.0, dtype=np.float32)
    delta_vv = np.zeros_like(post)
    delta_vh = np.zeros_like(post)
    post[2:7, 2:7] = -18
    delta_vv[2:7, 2:7] = -4
    delta_vh[2:7, 2:5] = -3
    scenario = DetectionScenario(vv_drop_db=-3, vh_drop_db=-2)

    evidence, combined = classify_evidence(post, delta_vv, delta_vh, -15, scenario, 2)

    assert combined.sum() == 25
    assert np.count_nonzero(evidence == 2) == 15
    assert np.count_nonzero(evidence == 1) == 10


def test_derive_threshold_is_clamped() -> None:
    rng = np.random.default_rng(7)
    values = np.concatenate(
        [rng.normal(-30, 0.2, 1000), rng.normal(-8, 0.2, 1000), rng.normal(-2, 0.2, 1000)]
    ).reshape(60, 50)

    threshold, method = derive_water_threshold(values, (-20, -12))

    assert threshold == -20
    assert method == "multi_otsu_3class_clamped"


def test_explicit_threshold_override_is_reported() -> None:
    threshold, method = derive_water_threshold(np.ones((2, 2)), (-20, -12), override=-16.5)

    assert threshold == -16.5
    assert method == "config_override"
