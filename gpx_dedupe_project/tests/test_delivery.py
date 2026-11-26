import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from route_semantics.city_pipeline import ingest
from route_semantics.delivery import apply_policy, select


def source_snapshot(path):
    # A prior writer may checkpoint WAL without changing logical evidence.
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        tables = [
            "routes",
            "sources",
            "metadata",
            "anchors",
            "route_anchors",
            "results",
            "audit",
            "route_bounds",
            "anchor_bounds",
        ]
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


class DeliveryTests(unittest.TestCase):
    def test_complete_pipeline_and_policy_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "source.db"
            output = root / "result"
            ingest(
                Path(__file__).resolve().parents[1]
                / "route_semantics/examples/synthetic.ndjson",
                database,
            )
            original = source_snapshot(database)
            report = select(database, output, "合成样例")
            self.assertEqual(report["total"], 14)
            self.assertEqual(report["thresholds"]["10"]["original"], 3)
            self.assertEqual(report["thresholds"]["10"]["fallback"], 1)
            self.assertEqual(report["exported_routes"], 3)
            geojson = json.loads((output / "routes.geojson").read_text())
            self.assertEqual(len(geojson["features"]), 3)
            self.assertTrue(
                all(
                    f["geometry"]["type"] == "MultiLineString"
                    for f in geojson["features"]
                )
            )
            self.assertEqual(source_snapshot(database), original)
            page = (output / "index.html").read_text()
            data = json.loads(
                page.split('<script type="application/json" id="data">')[1].split(
                    "</script>"
                )[0]
            )
            self.assertEqual(data["region"], "合成样例")
            self.assertEqual(data["initialPolicy"]["threshold"], 10)
            self.assertNotIn("__REGION_TITLE__", page)
            report = select(database, output, "合成样例", 20, 0.5, 300)
            self.assertEqual(report["thresholds"]["20"]["original"], 14)
            self.assertEqual(report["min_low_m"], 300)
            self.assertEqual(report["min_low_fraction"], 0.5)
            self.assertEqual(report["exported_routes"], 14)
            self.assertEqual(
                json.loads((output / "run-state.json").read_text())["state"],
                "succeeded",
            )
            with (
                patch(
                    "route_semantics.delivery.subprocess.run",
                    side_effect=RuntimeError("simulated Node failure"),
                ),
                self.assertRaises(RuntimeError),
            ):
                select(database, output)
            self.assertEqual(
                json.loads((output / "run-state.json").read_text())["state"], "failed"
            )
            self.assertTrue((output / "report.json").exists())
            self.assertEqual(source_snapshot(database), original)
            with self.assertRaises(ValueError):
                apply_policy(output / "coverage.sqlite3", {"records": [], "total": 0})
            with self.assertRaises(ValueError):
                select(database, output, threshold=-1)

    def test_invalid_multiline_preserves_previous_export(self):
        from route_semantics.city_store import CityStore, pack
        from route_semantics.delivery import export_geometries

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "source.db"
            output = Path(directory) / "routes.geojson"
            store = CityStore(database)
            with store.db:
                store.db.execute(
                    "INSERT INTO routes(id,parts) VALUES(1,?)",
                    (pack([[[104, 30], [104.01, 30]], [[104, 30]]]),),
                )
            store.db.close()
            output.write_text("previous valid export")
            with self.assertRaises(ValueError):
                export_geometries(
                    database, [{"id": 1, "keep": True, "kind": "original"}], output
                )
            self.assertEqual(output.read_text(), "previous valid export")
