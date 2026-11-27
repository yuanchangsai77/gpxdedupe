import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from route_semantics.city_pipeline import ingest
from route_semantics.delivery import apply_policy, reusable_density_report, select


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
            final_page = (output / "final-routes.html").read_text()
            embedded = json.loads(
                final_page.split('<script type="application/json" id="data">')[1].split(
                    "</script>"
                )[0]
            )
            self.assertEqual(embedded, geojson)
            self.assertNotIn("__GCJ__", final_page)
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
            self.assertTrue(report["density_cache_reused"])
            self.assertIsNotNone(
                reusable_density_report(database, output / "coverage.sqlite3")
            )
            with sqlite3.connect(output / "coverage.sqlite3") as cache:
                old_sha = cache.execute("SELECT sha FROM routes WHERE id=1").fetchone()[
                    0
                ]
                cache.execute("UPDATE routes SET sha='changed' WHERE id=1")
            self.assertIsNone(
                reusable_density_report(database, output / "coverage.sqlite3")
            )
            with sqlite3.connect(output / "coverage.sqlite3") as cache:
                cache.execute("UPDATE routes SET sha=? WHERE id=1", (old_sha,))
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

    def test_overlap_page_csv_manifest_geojson_and_database_agree(self):
        import csv
        import subprocess

        from route_semantics.city_store import pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "source.db"
            output = root / "result"
            ingest(
                Path(__file__).resolve().parents[1]
                / "route_semantics/examples/synthetic.ndjson",
                database,
            )
            shapes = {
                1: [[104.06, 30.67], [104.066, 30.67]],
                2: [[104.06, 30.67], [104.062, 30.67], [104.062, 30.672]],
                3: [[104.062, 30.67], [104.064, 30.67], [104.064, 30.672]],
                4: [[104.064, 30.67], [104.066, 30.67]],
            }
            with sqlite3.connect(database) as db:
                for rid, points in shapes.items():
                    db.execute(
                        "UPDATE routes SET state='candidate',rebuilt=?,definition=?,graph_version='test',algorithm_version='test' WHERE id=?",
                        (
                            pack([points]),
                            pack({"activity_type": "foot", "products": []}),
                            rid,
                        ),
                    )
            before = source_snapshot(database)
            report = select(
                database,
                output,
                "合成重建",
                overlap_mode="compact",
                overlap_fraction=0.5,
            )
            self.assertEqual(report["exported_routes"], 5)
            self.assertEqual(report["thresholds"]["10"]["rebuilt"], 3)
            expected = {2, 3, 4, 13, 14}
            with (output / "decisions.csv").open(encoding="utf-8-sig") as f:
                csv_ids = {
                    int(r["route_id"])
                    for r in csv.DictReader(f)
                    if r["selected"] == "1"
                }
            manifest_ids = {
                json.loads(line)["route_id"]
                for line in (output / "selected_routes.ndjson").read_text().splitlines()
            }
            geometry_ids = {
                f["properties"]["route_id"]
                for f in json.loads((output / "routes.geojson").read_text())["features"]
            }
            module = (
                Path(__file__).resolve().parents[1] / "route_semantics/final_policy.js"
            )
            script = "const fs=require('fs'),{evaluate}=require(process.argv[2]);const s=fs.readFileSync(process.argv[1],'utf8'),d=JSON.parse(s.split('<script type=\"application/json\" id=\"data\">')[1].split('</script>')[0]),p=d.initialPolicy,r=evaluate(d,{threshold:p.threshold,lowFraction:p.fraction,minimumMeters:p.minimum,overlapMode:p.overlapMode,overlapFraction:p.overlapFraction});console.log(JSON.stringify([...r.decisions].filter(([id,v])=>v.keep).map(([id])=>id)))"
            page_ids = set(
                json.loads(
                    subprocess.check_output(
                        ["node", "-e", script, str(output / "index.html"), str(module)],
                        text=True,
                    )
                )
            )
            with sqlite3.connect(output / "coverage.sqlite3") as db:
                database_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT route_id FROM density_decisions WHERE selected=1"
                    )
                }
                self.assertEqual(
                    db.execute("SELECT count(*) FROM overlap_metrics").fetchone()[0], 4
                )
            for actual in [csv_ids, manifest_ids, geometry_ids, page_ids, database_ids]:
                self.assertEqual(actual, expected)
            self.assertEqual(source_snapshot(database), before)
            report = select(database, output, overlap_mode="off")
            self.assertEqual(report["exported_routes"], 6)
            self.assertFalse(report["overlap"]["enabled"])
