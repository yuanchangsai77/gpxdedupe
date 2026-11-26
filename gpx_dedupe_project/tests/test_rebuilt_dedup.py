import unittest
from dataclasses import replace

from route_semantics.city_store import pack
from route_semantics.rebuilt_dedup import (
    Route,
    cluster,
    compare,
    cyclic_minimum,
    extract,
)


def row(points, edges, identifier=1):
    return {
        "id": identifier,
        "rebuilt": pack([points]),
        "definition": pack(
            {
                "source_parts": 1,
                "activity_type": "foot",
                "definitions": [
                    {"source_part": 0, "road_evidence": {"directed_edges": edges}}
                ],
            }
        ),
        "graph_version": "test",
        "algorithm_version": "test",
        "activity_codes": "[1]",
    }


class DedupTests(unittest.TestCase):
    def test_cycle_rotation_preserves_laps_and_direction(self):
        self.assertEqual(cyclic_minimum([3, 1, 2]), cyclic_minimum([1, 2, 3]))
        self.assertNotEqual(cyclic_minimum([1, 2, 3]), cyclic_minimum([3, 2, 1]))
        self.assertNotEqual(
            cyclic_minimum([1, 2, 3]), cyclic_minimum([1, 2, 3, 1, 2, 3])
        )

    def test_exact_and_sampling(self):
        p = [[104, 30], [104.001, 30], [104.002, 30]]
        a, _ = extract(row(p, [10, 20]))
        b, _ = extract(row([p[0], [104.0005, 30], p[1], p[2]], [10, 20], 2))
        self.assertTrue(compare(a, b)[0])
        c, _ = extract(
            row([[104, 30.0002], [104.001, 30.0002], [104.002, 30.0002]], [10, 20], 3)
        )
        self.assertFalse(compare(a, c)[0])

    def test_branch_extension_and_direction(self):
        p = [[104, 30], [104.001, 30], [104.002, 30]]
        a, _ = extract(row(p, [10, 20]))
        for edges in [
            [10, 30],
            [21, 11],
            [10, 20, 30],
            [10, 20, 21, 11, 10, 20, 21, 11],
        ]:
            b, _ = extract(row(p, edges, 2))
            self.assertFalse(compare(a, b)[0])
        self.assertFalse(compare(a, replace(a, length_m=a.length_m * 1.6))[0])

    def test_no_transitive_merge(self):
        routes = [Route(i, [], "same", False, 100, (0, i)) for i in range(3)]

        def near(a, b):
            return abs(a.id - b.id) <= 1, {}

        groups, _, _ = cluster(routes, near)
        self.assertEqual([len(g) for g in groups], [2, 1])

    def test_closed_different_start(self):
        p = [[104, 30], [104.001, 30], [104.001, 30.001], [104, 30]]
        q = [p[1], p[2], p[0], p[1]]
        a, _ = extract(row(p, [10, 20, 30, 10]))
        b, _ = extract(row(q, [20, 30, 10, 20], 2))
        self.assertTrue(compare(a, b)[0])

    def test_persistent_run(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from route_semantics.city_store import CityStore
        from route_semantics.rebuilt_dedup import run

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            coverage = Path(directory) / "coverage.db"
            output = Path(directory) / "dedup.db"
            store = CityStore(source)
            with store.db:
                for rid in (1, 2):
                    r = row([[104, 30], [104.001, 30], [104.002, 30]], [10, 20], rid)
                    store.db.execute(
                        "INSERT INTO routes(id,state,definition,rebuilt,graph_version,algorithm_version,activity_codes) VALUES(?,'candidate',?,?,?,?,?)",
                        (
                            rid,
                            r["definition"],
                            r["rebuilt"],
                            r["graph_version"],
                            r["algorithm_version"],
                            r["activity_codes"],
                        ),
                    )
            store.db.close()
            with sqlite3.connect(coverage) as db:
                db.execute(
                    "CREATE TABLE density_decisions(route_id INTEGER,selected INTEGER,geometry_kind TEXT)"
                )
                db.executemany(
                    "INSERT INTO density_decisions VALUES(?,1,'rebuilt')", [(1,), (2,)]
                )
            report = run(source, coverage, output)
            self.assertEqual(report["duplicate_routes"], 1)
            from route_semantics.rebuilt_dedup import source_snapshot

            store = CityStore(source, initialize=False)
            self.assertEqual(report["source_snapshot"], source_snapshot(store, {1, 2}))
            store.db.close()
            with sqlite3.connect(source) as db:
                db.execute("UPDATE routes SET algorithm_version='changed' WHERE id=1")
            store = CityStore(source, initialize=False)
            self.assertNotEqual(
                report["source_snapshot"], source_snapshot(store, {1, 2})
            )
            store.db.close()
            with sqlite3.connect(output) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM members").fetchone()[0], 2
                )

    def test_missing_edges_and_multipart_are_not_merged(self):
        r = row([[104, 30], [104.001, 30]], [])
        self.assertIsNone(extract(r)[0])
        r = row([[104, 30], [104.001, 30]], [1])
        r["rebuilt"] = pack([[[104, 30], [104.001, 30]], [[105, 30], [105.001, 30]]])
        self.assertIsNone(extract(r)[0])
