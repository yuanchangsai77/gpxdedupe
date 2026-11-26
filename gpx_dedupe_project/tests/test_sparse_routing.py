import copy
import unittest
from itertools import pairwise

from route_semantics.geometry import distance
from route_semantics.sparse_routing import (
    build,
    ordered_simplify,
    refinement,
    select_controls,
    validate_coverage,
)


class SparseRoutingTests(unittest.TestCase):
    def row(self, points):
        return {"id": "test", "original": points}

    def test_density_independent_straight_noise_has_no_interior_controls(self):
        sparse = [
            [104 + i * 0.0001, 30 + (0.00008 if i % 2 else -0.00008)]
            for i in range(101)
        ]
        dense = []
        for a, b in pairwise(sparse):
            dense.extend(
                [[a[k] + (b[k] - a[k]) * j / 10 for k in (0, 1)] for j in range(10)]
            )
        dense.append(sparse[-1])
        self.assertEqual(len(select_controls(sparse)[1]), 2)
        self.assertEqual(len(select_controls(dense)[1]), 2)

    def test_isolated_drift_not_a_forced_side_trip(self):
        points = [[104 + i * 0.00005, 30] for i in range(101)]
        points[50][1] += 0.0004
        evidence, indices, _ = select_controls(points)
        self.assertEqual(len(indices), 2)
        self.assertNotIn(points[50], [evidence[i] for i in indices])

    def test_loop_and_repeated_outback_preserve_temporal_controls(self):
        points = [[104, 30], [104.004, 30], [104.004, 30.004], [104, 30.004], [104, 30]]
        _evidence, indices, _ = select_controls(points)
        self.assertGreaterEqual(len(indices), 5)
        outback = [[104, 30], [104.008, 30], [104, 30], [104.008, 30], [104, 30]]
        _evidence, indices, _ = select_controls(outback)
        self.assertGreaterEqual(len(indices), 5)
        self.assertEqual(indices, sorted(indices))

    def test_failure_never_inserts_original_or_snap_geometry(self):
        row = self.row([[104, 30], [104.001, 30], [104.002, 30]])
        snapshot = copy.deepcopy(row)

        class Missing:
            def route(self, controls, mode):
                raise ValueError("missing_road")

        result = validate_coverage(build(row, Missing()), row["original"])
        self.assertEqual(result["segments"], [])
        self.assertEqual(row, snapshot)
        self.assertFalse(result["navigation_enabled"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_native_snapped_endpoints_stay_on_road_without_connectors(self):
        row = self.row([[104, 30], [104.001, 30], [104.002, 30]])

        class Snap:
            graph_version = "test"

            def route(self, controls, mode):
                self.controls = controls
                assert mode == "foot"
                return {
                    "geometry": {
                        "coordinates": [[p[0], p[1] + 0.00005] for p in controls]
                    }
                }

        adapter = Snap()
        result = validate_coverage(build(row, adapter), row["original"])
        self.assertEqual(result["mode"], "road_candidate")
        self.assertEqual([s["kind"] for s in result["segments"]], ["road"])
        self.assertEqual(len(adapter.controls), 2)
        self.assertGreater(
            distance(
                result["segments"][0]["geometry"]["coordinates"][-1],
                row["original"][-1],
            ),
            1,
        )

    def test_detour_and_lost_laps_rejected_without_raw_fallback(self):
        row = self.row([[104, 30], [104.004, 30], [104, 30], [104.004, 30], [104, 30]])

        class Shortcut:
            graph_version = "test"

            def route(self, controls, mode):
                return {
                    "geometry": {"coordinates": [[104, 30], [104.004, 30], [104, 30]]}
                }

        result = build(row, Shortcut())
        self.assertEqual(result["segments"], [])
        self.assertNotEqual(result["mode"], "road_candidate")

    def test_only_sustained_disagreement_can_refine(self):
        evidence = [[104 + i * 0.00015, 30] for i in range(40)]
        isolated = [0] * 40
        isolated[20] = 100
        self.assertIsNone(refinement(evidence, [0, 39], isolated))
        sustained = [100 if 12 <= i <= 24 else 0 for i in range(40)]
        i = refinement(evidence, [0, 39], sustained)
        self.assertIn(i, range(12, 25))

    def test_wrongly_snapped_noisy_control_is_removed_before_adding_more(self):
        # Synthetic drift excursion near an otherwise straight road. The mock
        # engine snaps its high point to a distant spur, modeling the reported bug.
        points = [
            [104 + i * 0.0001, 30 + max(0, 1 - abs(i - 100) / 5) * 0.0007]
            for i in range(201)
        ]

        class WrongSnap:
            graph_version = "synthetic"

            def route(self, controls, mode):
                if any(p[1] > 30.0005 for p in controls):
                    return {
                        "geometry": {
                            "coordinates": [controls[0], [104.01, 30.005], controls[-1]]
                        }
                    }
                return {"geometry": {"coordinates": [controls[0], controls[-1]]}}

        result = build(self.row(points), WrongSnap())
        self.assertEqual(result["mode"], "road_candidate")
        self.assertLess(len(result["controls"]), result["initial_control_count"])
        self.assertEqual(result["attempts"][-1]["operation"], "test_remove_control")
        self.assertFalse(
            any(
                a["operation"] == "test_add_supported_branch_control"
                for a in result["attempts"]
            )
        )
        self.assertTrue(all(s["kind"] == "road" for s in result["segments"]))

    def test_ordered_simplification_preserves_reversals_inside_straight_chord(self):
        points = [[104, 30], [104.008, 30], [104.003, 30], [104.012, 30]]
        self.assertEqual(ordered_simplify(points), [0, 1, 2, 3])

    def test_facility_without_road_has_only_original_as_comparison(self):
        row = {"id": "chengdu-07", "original": [[104, 30], [104.001, 30], [104, 30]]}
        result = build(row, None)
        self.assertEqual(result["mode"], "facility_unavailable")
        self.assertEqual(result["segments"], [])
