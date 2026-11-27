import unittest

from route_semantics.overlap_index import make_index


def route(identifier, points, scope="foot"):
    return {"id": identifier, "parts": [points], "scope": scope}


class OverlapIndexTests(unittest.TestCase):
    def test_partial_collinear_and_reverse_overlap(self):
        index = make_index(
            [
                route(1, [[104, 30], [104.002, 30]]),
                route(2, [[104.001, 30], [104.003, 30]]),
                route(3, [[104.002, 30], [104.001, 30]]),
            ]
        )
        a, b, c = [set(index["routes"][str(i)]["atoms"]) for i in [1, 2, 3]]
        self.assertEqual(len(a & b), 1)
        self.assertEqual(a & b, c)
        self.assertGreater(sum(index["weights"][i] for i in a - b), 90)

    def test_parallel_roads_and_graph_scope_do_not_overlap(self):
        index = make_index(
            [
                route(1, [[104, 30], [104.002, 30]]),
                route(2, [[104, 30.00001], [104.002, 30.00001]]),
                route(3, [[104, 30], [104.002, 30]], "other-graph"),
            ]
        )
        a = set(index["routes"]["1"]["atoms"])
        self.assertFalse(a & set(index["routes"]["2"]["atoms"]))
        self.assertFalse(a & set(index["routes"]["3"]["atoms"]))

    def test_ring_protection_excludes_plain_out_and_back(self):
        ring = [[104, 30], [104.003, 30], [104.003, 30.003], [104, 30.003], [104, 30]]
        out = [[104, 30], [104.003, 30], [104, 30]]
        index = make_index(
            [
                route(1, ring),
                route(2, out),
                route(3, ring + ring[1:]),
                route(4, ring[:-1] + [[104, 30.0001]]),
            ]
        )
        self.assertTrue(index["routes"]["1"]["protect"])
        self.assertIsNone(index["routes"]["2"]["protect"])
        self.assertTrue(index["routes"]["3"]["protect"])
        self.assertTrue(index["routes"]["4"]["protect"])

    def test_source_cannot_be_overwritten(self):
        import tempfile
        from pathlib import Path

        from route_semantics.overlap_index import run

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            source.write_text("original evidence")
            with self.assertRaises(ValueError):
                run(source, source)
            self.assertEqual(source.read_text(), "original evidence")

    def test_selection_ids_contain_only_selected_rebuilt(self):
        import json
        import sqlite3
        import tempfile
        from pathlib import Path

        from route_semantics.city_store import CityStore, pack
        from route_semantics.overlap_index import run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            coverage = root / "coverage.db"
            output = root / "index.json"
            store = CityStore(source)
            with store.db:
                for rid in (1, 2):
                    store.db.execute(
                        "INSERT INTO routes(id,state,definition,rebuilt,graph_version,algorithm_version) VALUES(?,'candidate',?,?,?,?)",
                        (
                            rid,
                            pack({"activity_type": "foot"}),
                            pack([[[104, 30], [104.002, 30]]]),
                            "test",
                            "test",
                        ),
                    )
            store.db.close()
            with sqlite3.connect(coverage) as db:
                db.execute(
                    "CREATE TABLE density_decisions(route_id,selected,geometry_kind)"
                )
                db.executemany(
                    "INSERT INTO density_decisions VALUES(?,1,?)",
                    [(1, "rebuilt"), (2, "original")],
                )
            run(source, output, coverage)
            self.assertEqual(
                json.loads((root / "overlap-selected-ids.json").read_text()), [1]
            )
