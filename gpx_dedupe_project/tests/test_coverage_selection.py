import tempfile
import unittest
from pathlib import Path

from route_semantics.city_store import CityStore, pack
from route_semantics.coverage import build
from route_semantics.coverage_selection import geometry_choice, run
from route_semantics.coverage_view import export


class SelectionTests(unittest.TestCase):
    def test_geometry_policy(self):
        self.assertEqual(geometry_choice("eligible", "candidate", True)[0], "rebuilt")
        for state in ["failed", "review", "resource_limited", "queued"]:
            self.assertEqual(geometry_choice("eligible", state, True)[0], "original")
        self.assertEqual(geometry_choice("review", "candidate", True)[0], "none")

    def test_sparse_preservation_and_dense_reduction(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "source.db"
            coverage = Path(d) / "coverage.db"
            store = CityStore(source)
            with store.db:
                for rid in range(1, 5):
                    store.db.execute(
                        "INSERT INTO routes(id,content_sha256,parts,state,rebuilt) VALUES(?,?,?,?,?)",
                        (
                            rid,
                            str(rid),
                            pack([[[104, 30], [104.001, 30]]]),
                            "candidate" if rid in (1, 2) else "failed",
                            pack([[[104, 30], [104.001, 30]]])
                            if rid in (1, 2)
                            else None,
                        ),
                    )
                store.db.execute(
                    "INSERT INTO routes(id,content_sha256,parts,state) VALUES(?,?,?,?)",
                    (5, "5", pack([[[105, 30], [105.001, 30]]]), "failed"),
                )
            store.db.close()
            build(source, coverage, 5)
            result = run(source, coverage)
            self.assertEqual(result["selected"], 3)
            self.assertEqual(result["selected_original"], 1)
            self.assertEqual(result["selected_rebuilt"], 2)
            import sqlite3

            from route_semantics.coverage_rollup import rollup

            self.assertGreater(rollup(coverage), 0)
            with sqlite3.connect(coverage) as db:
                small = db.execute(
                    "SELECT sum(length_m) FROM contributions WHERE size=100"
                ).fetchone()[0]
                large = db.execute(
                    "SELECT sum(length_m) FROM contributions WHERE size=5000"
                ).fetchone()[0]
                self.assertAlmostEqual(small, large)
            page = Path(d) / "index.html"
            self.assertEqual(export(source, coverage, page)["routes"], 5)
            self.assertNotIn("__DATA__", page.read_text())
            self.assertIn("混合表达", page.read_text())
            self.assertEqual(run(source, coverage)["selected"], 3)
            with sqlite3.connect(coverage) as db:
                db.execute("INSERT INTO routes VALUES(99,'99',1,0,'test')")
            with self.assertRaises(ValueError):
                export(source, coverage, page)
            with self.assertRaises(ValueError):
                run(source, coverage)
