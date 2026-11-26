import copy
import io
import json
import unittest
from urllib.error import HTTPError

from route_semantics.city_pipeline import windows
from route_semantics.endpoint_recovery import EndpointRecovery, endpoint_candidates
from route_semantics.failure_diagnosis import diagnose
from route_semantics.sparse_routing import build, quality, resample


def missing(index):
    return HTTPError(
        "http://localhost/route",
        400,
        "Bad request",
        {},
        io.BytesIO(
            json.dumps(
                {"hints": [{"details": "PointNotFoundException", "point_index": index}]}
            ).encode()
        ),
    )


class EndpointRecoveryTests(unittest.TestCase):
    def test_endpoint_candidates_bounded_by_distance_and_original_indices(self):
        points = [[104 + i * 0.0002, 30] for i in range(10)]
        candidates = endpoint_candidates(points)
        self.assertEqual([c["source_index"] for c in candidates], [0, 1, 2, 3])
        self.assertLessEqual(candidates[-1]["omitted_observed_m"], 60)
        self.assertEqual(endpoint_candidates(points, True)[1]["source_index"], 8)
        self.assertEqual(len(endpoint_candidates([[104, 30], [105, 30], [106, 30]])), 1)

    def test_endpoint_recovery_records_actual_planning_points(self):
        source = [[104, 30], [104.0001, 30], [104.001, 30]]
        snapshot = copy.deepcopy(source)

        class Engine:
            graph_version = "test"
            route_request_count = 0

            def route(self, points, mode):
                self.route_request_count += 1
                if points[0] == source[0]:
                    raise missing(0)
                return {"geometry": {"coordinates": points}}

        recovered = build(
            {"id": "test", "original": source}, EndpointRecovery(Engine(), source)
        )
        self.assertEqual(recovered["mode"], "road_candidate")
        self.assertEqual(recovered["controls"][0]["coordinate"], source[1])
        self.assertEqual(recovered["controls"][0]["observed_coordinate"], source[0])
        self.assertLess(recovered["road_observation_coverage"], 1)
        self.assertEqual(source, snapshot)
        self.assertEqual(
            recovered["segments"][0]["geometry"]["coordinates"][0], source[1]
        )

    def test_missing_interior_does_not_retry_endpoints(self):
        class Engine:
            graph_version = "g"
            route_request_count = 0

            def route(self, points, mode):
                self.route_request_count += 1
                raise missing(1)

        engine = Engine()
        adapter = EndpointRecovery(engine, [[104, 30], [104.0001, 30], [104.001, 30]])
        with self.assertRaises(HTTPError):
            adapter.route(adapter.source, "foot")
        self.assertEqual(engine.route_request_count, 1)

    def test_bounded_expanded_snap_requires_subsequent_foot_route(self):
        source = [[104, 30], [104.001, 30]]

        class Engine:
            graph_version = "g"
            route_request_count = 0

            def nearest(self, point):
                return {"coordinates": [104.0002, 30], "distance": 19}

            def route(self, points, mode):
                self.route_request_count += 1
                if points[0] == source[0]:
                    raise missing(0)
                return {"geometry": {"coordinates": points}}

        result = EndpointRecovery(Engine(), source).route(source, "foot")
        self.assertEqual(result["planning_controls"][0], [104.0002, 30])
        self.assertEqual(
            result["endpoint_adjustments"][0]["planning_start"]["snap_distance_m"], 19
        )

    def test_expanded_snap_does_not_accept_far_road(self):
        source = [[104, 30], [104.001, 30]]

        class Engine:
            graph_version = "g"
            route_request_count = 0

            def nearest(self, point):
                return {"coordinates": [105, 30], "distance": 1000}

            def route(self, points, mode):
                self.route_request_count += 1
                raise missing(0)

        engine = Engine()
        with self.assertRaises(HTTPError):
            EndpointRecovery(engine, source).route(source, "foot")
        self.assertEqual(engine.route_request_count, 1)

    def test_long_valid_route_completes_bounded_windows(self):
        points = [[104 + i * 0.001, 30] for i in range(1001)]

        class Engine:
            graph_version = "g"

            def route(self, points, mode):
                return {"geometry": {"coordinates": points}}

        chunks = list(windows(points, 8000))
        self.assertGreater(len(chunks), 10)
        for window in chunks:
            p = build({"id": "long-synthetic", "original": window}, Engine())
            self.assertEqual(p["mode"], "road_candidate")

    def test_large_detour_is_rejected_without_sampling_exception(self):
        evidence = resample([[104, 30], [104.06, 30]], 15)
        geometry = [[104, 30], [105, 30], [104.06, 30]]
        accepted, metrics, _ = quality(evidence, geometry)
        self.assertFalse(accepted)
        self.assertTrue(metrics["coarse_sampling_for_rejection_only"])
        self.assertGreater(metrics["distance_ratio"], 1.35)

    def test_resource_failure_is_not_misreported_as_missing_road(self):
        self.assertEqual(
            diagnose(
                {
                    "mode": "road_unavailable",
                    "metrics": None,
                    "attempts": [{"reason": "sampling_resource_limit"}],
                }
            )["category"],
            "validation_resource_limit",
        )
