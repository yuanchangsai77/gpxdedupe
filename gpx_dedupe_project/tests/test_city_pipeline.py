import json
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import numpy as np

from route_semantics import fast_geometry, sparse_routing
from route_semantics.city_pipeline import ingest, windows
from route_semantics.city_store import CityStore, unpack
from route_semantics.geometry import point_segment


class CityTests(unittest.TestCase):
    def test_missing_graph_version_cannot_create_or_requeue_a_database(self):
        from route_semantics.city_pipeline import reconstruct
        from route_semantics.city_rebuild import rebuild

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "city.db"
            for version in (None, "", " "):
                with self.assertRaisesRegex(ValueError, "graph_version_required"):
                    reconstruct(database, "http://localhost", version)
                with self.assertRaisesRegex(ValueError, "graph_version_required"):
                    rebuild(database, 1, version)
            self.assertFalse(database.exists())
            self.assertFalse(Path(str(database) + ".runner.lock").exists())

    def test_gallery_embedded_json_escapes_markup_without_changing_titles(self):
        from route_semantics.build_gallery import build as gallery

        with tempfile.TemporaryDirectory() as temp:
            title = "岸边入口 </script><script>bad()</script>"
            row = {
                "id": "synthetic",
                "title": title,
                "original": [[104, 30], [104.001, 30]],
                "reconstructed": [],
                "original_m": 100,
                "reconstructed_m": None,
                "edge_count": 0,
                "attempt_count": 0,
            }
            source, output = Path(temp) / "input.json", Path(temp) / "output.html"
            source.write_text(json.dumps({"routes": [row]}))
            gallery(source, output)
            page = output.read_text()
            embedded = page.split('<script id="route-data" type="application/json">')[
                1
            ].split("</script>")[0]
            self.assertEqual(json.loads(embedded)["routes"][0]["title"], title)
            self.assertNotIn("</script><script>bad()", page)

    def test_malformed_records_do_not_abort_ingestion_or_advance_past_valid_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input.ndjson"
            values = [[], None, {"segments": [None]}, {"segments": {}}]
            values.append(
                {
                    "segments": [
                        {
                            "routePoints": [
                                {"longitude": 104, "latitude": 30},
                                {"longitude": 104.001, "latitude": 30},
                            ]
                        }
                    ]
                }
            )
            source.write_text("".join(json.dumps(v) + "\n" for v in values))
            snapshot = source.read_bytes()
            result = ingest(source, Path(temp) / "city.db")
            self.assertEqual(result["states"], {"invalid": 4, "queued": 1})
            self.assertEqual(result["sources"][0]["offset"], len(snapshot))
            self.assertTrue(result["sources"][0]["complete"])
            self.assertEqual(source.read_bytes(), snapshot)

    def test_numeric_kernels_ignore_elevation_like_reference_geometry(self):
        a = [[104, 30], [104.001, 30.001], [104.002, 30]]
        b = [[104, 30], [104.001, 30.0005], [104.002, 30]]
        elevated_a = [p + [100 + i] for i, p in enumerate(a)]
        elevated_b = [p + [200 + i] for i, p in enumerate(b)]
        np.testing.assert_allclose(
            fast_geometry.corridor(elevated_a, elevated_b), fast_geometry.corridor(a, b)
        )
        self.assertEqual(
            fast_geometry.ordered(elevated_a, elevated_b), fast_geometry.ordered(a, b)
        )

    def test_numeric_kernels_reject_malformed_input_before_native_call(self):
        for points in ([[104], [105]], [[104, float("nan")]], [[[104, 30]]]):
            with self.assertRaises(ValueError):
                fast_geometry.corridor(points, [[104, 30], [104.001, 30]])

    def test_finalization_cannot_race_with_an_active_runner(self):
        import fcntl

        from route_semantics.city_finalize import finalize

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "city.db"
            with open(str(database) + ".runner.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    finalize(database)
                self.assertFalse(database.exists())

    def test_dense_observations_can_rebuild_from_stored_sparse_controls(self):
        from route_semantics.city_rebuild import rebuild
        from route_semantics.city_store import pack

        a, b = [104, 30], [104.02, 30]
        observations = [[104 + 0.02 * i / 19999, 30] for i in range(20000)]

        class Engine:
            calls = 0

            def route(self, controls, mode):
                self.calls += 1
                return {"geometry": {"coordinates": controls}}

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "city.db"
            store = CityStore(database)
            definition = {
                "definitions": [
                    {
                        "start_anchor": {"coordinate": a},
                        "ordered_segments": [
                            {"from": {"coordinate": a}, "to": {"coordinate": b}}
                        ],
                    }
                ]
            }
            with store.db:
                store.db.execute(
                    "INSERT INTO routes(id,state,parts,definition,graph_version) VALUES(1,'failed',?,?,'g')",
                    (pack([observations]), pack(definition)),
                )
            engine = Engine()
            result = rebuild(database, 1, "g", force=True, adapter=engine)
            self.assertEqual(engine.calls, 1)
            self.assertEqual(result["metadata"]["state"], "candidate")
            self.assertTrue(result["features"])

    def test_accelerated_distances_equivalent_to_reference(self):
        rng = np.random.default_rng(42)
        a = (rng.random((37, 2)) * 0.01 + [104, 30]).tolist()
        b = (rng.random((29, 2)) * 0.01 + [104, 30]).tolist()
        expected = [min(point_segment(p, x, y) for x, y in pairwise(b)) for p in a]
        np.testing.assert_allclose(
            fast_geometry.corridor(a, b), expected, rtol=1e-9, atol=1e-6
        )

    def test_native_frechet_score_and_backtrace_equivalent(self):
        rng = np.random.default_rng(17)
        for n, m in [(1, 1), (3, 8), (20, 17), (50, 50)]:
            a = (rng.random((n, 2)) * 0.01 + [104, 30]).tolist()
            b = (rng.random((m, 2)) * 0.01 + [104, 30]).tolist()
            with patch.object(sparse_routing, "fast_ordered", return_value=None):
                ref = sparse_routing.ordered_residuals(a, b)
            got = fast_geometry.ordered(a, b)
            self.assertIsNotNone(got)
            np.testing.assert_allclose(got[0], ref[0], rtol=1e-9, atol=1e-6)
            np.testing.assert_allclose(got[1], ref[1], rtol=1e-9, atol=1e-6)

    def test_ingestion_is_idempotent_and_preserves_segment_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "data.ndjson"
            db = Path(temp) / "city.sqlite3"
            points = lambda x: [
                {"longitude": x, "latitude": 30},
                {"longitude": x + 0.001, "latitude": 30},
            ]
            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"routePoints": points(104)},
                            {"routePoints": points(105)},
                        ]
                    }
                )
                + "\n"
                + json.dumps({"segments": []})
                + "\n"
            )
            snapshot = source.read_bytes()
            ingest(source, db)
            ingest(source, db)
            store = CityStore(db)
            self.assertEqual(store.summary()["total"], 2)
            row = store.db.execute("SELECT * FROM routes WHERE id=1").fetchone()
            self.assertEqual(len(unpack(row["parts"])), 2)
            self.assertLess(row["distance_m"], 300)
            self.assertEqual(source.read_bytes(), snapshot)

    def test_long_windows_overlap_only_at_native_boundary(self):
        p = [[104 + i * 0.001, 30] for i in range(300)]
        parts = list(windows(p, maximum_m=3000))
        self.assertGreater(len(parts), 5)
        self.assertTrue(all(a[-1] == b[0] for a, b in pairwise(parts)))

    def test_private_controls_do_not_merge_by_nearby_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CityStore(Path(temp) / "city.db")
            with db.db:
                db.db.execute("INSERT INTO routes(id) VALUES(1)")
                db.db.execute("INSERT INTO routes(id) VALUES(2)")
                for i in (1, 2):
                    db.link_controls(
                        i,
                        "g",
                        "v",
                        [
                            {
                                "controls": [
                                    {"coordinate": [104, 30]},
                                    {"coordinate": [104.001, 30]},
                                ]
                            }
                        ],
                    )
            self.assertEqual(
                db.db.execute(
                    "SELECT count(distinct anchor_id) FROM route_anchors"
                ).fetchone()[0],
                4,
            )

    def test_shared_osm_entity_keeps_repeated_visits_as_separate_refs(self):
        with tempfile.TemporaryDirectory() as temp:
            db = CityStore(Path(temp) / "city.db")
            with db.db:
                db.db.execute("INSERT INTO routes(id) VALUES(1)")
                db.put_anchor("osm:node:1", [104, 30], source="OpenStreetMap")
                db.link_controls(
                    1,
                    "g",
                    "v",
                    [
                        {
                            "controls": [
                                {"coordinate": [104, 30]},
                                {"coordinate": [104, 30]},
                            ]
                        }
                    ],
                )
            self.assertEqual(
                db.db.execute("SELECT count(*) FROM route_anchors").fetchone()[0], 2
            )
            self.assertEqual(
                db.db.execute(
                    "SELECT count(distinct anchor_id) FROM route_anchors"
                ).fetchone()[0],
                1,
            )

    def test_corridor_pruning_never_promotes_invalid_path(self):
        points = [[104, 30], [104.001, 30], [104.002, 30]]
        passed, metrics, residuals = sparse_routing.quality(
            points, [[104, 30], [104.02, 30.02], [104.002, 30]], incumbent=1.2
        )
        self.assertFalse(passed)
        self.assertIsNone(residuals)
        self.assertIsNone(metrics["ordered_error_m"])

    def test_exact_pruning_does_not_skip_potentially_accepted_path(self):
        points = [[104, 30], [104.001, 30], [104.002, 30]]
        passed, _metrics, residuals = sparse_routing.quality(
            points, points, incumbent=0.1
        )
        self.assertTrue(passed)
        self.assertIsNotNone(residuals)

    def test_batch_resume_graph_revisions_and_final_source_verification(self):
        from route_semantics import city_pipeline
        from route_semantics.city_finalize import finalize

        class Engine:
            def __init__(self, url, graph):
                self.graph_version = graph
                self.route_request_count = 0

            def wait_ready(self):
                pass

            def route(self, controls, mode):
                self.route_request_count += 1
                return {
                    "geometry": {"coordinates": controls},
                    "osm_way_ids": [123],
                    "directed_edges": [2],
                }

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.ndjson"
            database = Path(temp) / "city.db"
            row = {
                "segments": [
                    {
                        "routePoints": [
                            {"longitude": 104 + i * 0.0001, "latitude": 30}
                            for i in range(20)
                        ]
                    }
                ]
            }
            source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            snapshot = source.read_bytes()
            ingest(source, database)
            with patch.object(city_pipeline, "GraphHopperAdapter", Engine):
                city_pipeline.reconstruct(
                    database, "http://localhost", "g1", workers=2, limit=1
                )
                self.assertEqual(
                    CityStore(database).summary()["states"].get("queued"), 1
                )
                city_pipeline.reconstruct(database, "http://localhost", "g1", workers=2)
                self.assertEqual(
                    CityStore(database).summary()["states"].get("candidate"), 2
                )
                city_pipeline.reconstruct(database, "http://localhost", "g2", workers=2)
            store = CityStore(database)
            self.assertEqual(
                store.db.execute("SELECT count(*) FROM results").fetchone()[0], 4
            )
            report = finalize(database)
            self.assertTrue(
                report["source_integrity"][0]["record_hashes_match_ingestion"]
            )
            self.assertEqual(source.read_bytes(), snapshot)
            record = store.db.execute(
                "SELECT definition FROM routes WHERE id=1"
            ).fetchone()
            ir = unpack(record[0])
            self.assertTrue(ir["route_semantic_id"])
            self.assertEqual(
                ir["ordered_segments"][-1]["to_anchor_id"], ir["end_anchor"]["id"]
            )
            self.assertFalse(ir["geometry_constraints"]["raw_trace_fallback"])
            self.assertIsNone(ir["confidence"])

    def test_failed_extraction_retains_observed_endpoints_without_fake_geometry(self):
        from route_semantics.city_finalize import finalize
        from route_semantics.city_store import pack

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.ndjson"
            database = Path(temp) / "city.db"
            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "routePoints": [
                                    {"longitude": 104, "latitude": 30},
                                    {"longitude": 104.001, "latitude": 30},
                                ]
                            }
                        ]
                    }
                )
                + "\n"
            )
            ingest(source, database)
            store = CityStore(database)
            with store.db:
                store.db.execute(
                    "UPDATE routes SET state='failed',graph_version='g',algorithm_version='a',definition=?",
                    (pack({"error": "synthetic extraction failure", "products": []}),),
                )
            finalize(database)
            r = store.db.execute("SELECT definition,rebuilt FROM routes").fetchone()
            ir = unpack(r[0])
            self.assertEqual(ir["start_anchor"]["coordinate"], [104, 30])
            self.assertEqual(ir["end_anchor"]["coordinate"], [104.001, 30])
            self.assertIsNone(r[1])
            self.assertEqual(
                ir["ordered_segments"][-1]["constraint_status"],
                "unresolved_source_observation",
            )
            self.assertIsNone(ir["core_fingerprint"])
            self.assertGreater(
                store.db.execute("SELECT count(*) FROM route_anchors").fetchone()[0], 0
            )

    def test_on_demand_rebuild_reuses_controls_and_cache_without_extraction(self):
        from route_semantics.city_rebuild import rebuild
        from route_semantics.city_store import pack

        a, b = [104, 30], [104.001, 30]

        class Engine:
            def __init__(self):
                self.calls = []

            def route(self, controls, mode):
                self.calls.append((controls, mode))
                return {"geometry": {"coordinates": controls}}

        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "city.db"
            store = CityStore(db)
            definition = {
                "definitions": [
                    {
                        "start_anchor": {"coordinate": a},
                        "ordered_segments": [
                            {"from": {"coordinate": a}, "to": {"coordinate": b}}
                        ],
                    }
                ],
                "version_fingerprint": "synthetic-definition",
            }
            with store.db:
                store.db.execute(
                    "INSERT INTO routes(id,content_sha256,state,parts,definition,rebuilt,graph_version,activity_codes) VALUES(1,'synthetic','candidate',?,?,?,'g','[]')",
                    (
                        pack([[a, [104.0005, 30.00005], b]]),
                        pack(definition),
                        pack([[a, b]]),
                    ),
                )
            engine = Engine()
            cached = rebuild(db, 1, "g", adapter=engine)
            self.assertEqual(engine.calls, [])
            self.assertTrue(cached["metadata"]["geometry_from_cache"])
            result = rebuild(db, 1, "g", force=True, adapter=engine)
            self.assertEqual(engine.calls, [([a, b], "foot")])
            self.assertEqual(result["metadata"]["routing_requests"], 1)
            self.assertTrue(result["metadata"]["used_stored_definition"])
            self.assertFalse(result["metadata"]["raw_trace_fallback"])

            from route_semantics.adapters import UnsupportedConstraint

            for scope in (
                definition,
                definition["definitions"][0]["ordered_segments"][0],
            ):
                scope["mandatory_edges"] = ["synthetic-required-edge"]
                with store.db:
                    store.db.execute(
                        "UPDATE routes SET definition=? WHERE id=1", (pack(definition),)
                    )
                with self.assertRaises(UnsupportedConstraint):
                    rebuild(db, 1, "g", adapter=engine)
                scope.pop("mandatory_edges")
