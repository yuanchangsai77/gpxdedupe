import json
import tempfile
import unittest
from pathlib import Path

from route_semantics.city_store import CityStore, pack
from route_semantics.coverage import build
from route_semantics.coverage_view import export


class CoverageViewTests(unittest.TestCase):
    def test_rebuilt_uses_full_geometry_instead_of_overview_shortcuts(self):
        full = [
            [104.070001, 30.585001],
            [104.070001, 30.585501],
            [104.070501, 30.585501],
            [104.070501, 30.585001],
        ]
        coarse = [full[0], full[-1]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            coverage = root / "coverage.db"
            page = root / "view.html"
            store = CityStore(source)
            with store.db:
                store.db.execute(
                    "INSERT INTO routes(id,parts,display,state,rebuilt,rebuilt_display,content_sha256) VALUES(1,?,?,'candidate',?,?,?)",
                    (
                        pack([full]),
                        pack([coarse]),
                        pack([full]),
                        pack([coarse]),
                        "test",
                    ),
                )
            store.db.close()
            build(source, coverage, 1, sizes=(5000,))
            export(source, coverage, page)
            data = json.loads(
                page.read_text()
                .split('<script type="application/json" id="data">')[1]
                .split("</script>")[0]
            )
            self.assertEqual(data["routes"][0]["rebuilt"], [full])
            self.assertEqual(len(data["routes"][0]["parts"][0]), 2)

            stale = root / "overlap.json"
            stale.write_text(json.dumps({"route_ids": [1], "source_snapshot": "stale"}))
            previous = page.read_text()
            with self.assertRaises(ValueError):
                export(source, coverage, page, overlap=stale)
            self.assertEqual(page.read_text(), previous)
