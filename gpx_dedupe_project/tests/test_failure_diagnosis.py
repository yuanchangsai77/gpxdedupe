import unittest

from route_semantics.failure_diagnosis import diagnose


class FailureDiagnosisTests(unittest.TestCase):
    def product(self, **changes):
        metrics = {
            "forward_p95_m": 55,
            "reverse_p95_m": 48,
            "maximum_corridor_error_m": 64,
            "ordered_error_m": 70,
            "distance_ratio": 1.1,
            "endpoint_snap_m": 45,
        }
        metrics.update(changes)
        return {"mode": "road_unavailable", "metrics": metrics}

    def test_near_candidate_is_reviewable_not_accepted(self):
        p = self.product()
        self.assertTrue(diagnose(p)["review_preview"])
        self.assertEqual(p["mode"], "road_unavailable")

    def test_major_detour_not_auto_previewed(self):
        d = diagnose(self.product(reverse_p95_m=500, distance_ratio=2.7))
        self.assertFalse(d["review_preview"])
        self.assertEqual(d["category"], "unsupported_detour")

    def test_shortcut_not_misreported_as_connection_failure(self):
        self.assertEqual(
            diagnose(self.product(distance_ratio=0.56))["category"], "missing_journey"
        )

    def test_unsnappable_control_uses_engine_evidence(self):
        p = {
            "mode": "road_unavailable",
            "metrics": None,
            "attempts": [{"engine_error": "PointNotFoundException"}],
        }
        self.assertEqual(diagnose(p)["category"], "unsnappable_control")

    def test_no_connection_not_claimed_as_real_world_prohibition(self):
        p = {"mode": "road_unavailable", "metrics": None, "attempts": []}
        self.assertIn("不能据此断言", diagnose(p)["message"])
