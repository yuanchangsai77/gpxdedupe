import tempfile
import unittest
from pathlib import Path

from route_semantics.city_store import CityStore, pack
from route_semantics.coverage import build, measure, select


class CoverageTests(unittest.TestCase):
    def test_sampling_density_and_laps(self):
        a, b = [104, 30], [104.001, 30]
        dense = [[104 + i * 0.00001, 30] for i in range(101)]
        coarse, length, _ = measure([[a, b]])
        fine, other, _ = measure([dense])
        self.assertAlmostEqual(length, other, places=6)
        self.assertLess(
            sum(
                abs(coarse.get(k, 0) - fine.get(k, 0)) for k in set(coarse) | set(fine)
            ),
            40,
        )
        _, twice, _ = measure([[a, b, a]])
        self.assertAlmostEqual(twice, length * 2)

    def test_gap_and_parts(self):
        cells, length, gap = measure([[[104, 30], [105, 30]]])
        self.assertEqual(cells, {})
        self.assertEqual(length, 0)
        self.assertGreater(gap, 1000)
        self.assertEqual(measure([[[104, 30]], [[105, 30]]])[1], 0)

    def test_resume_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            output = Path(directory) / "coverage.db"
            store = CityStore(source)
            with store.db:
                for i in (1, 2):
                    store.db.execute(
                        "INSERT INTO routes(id,content_sha256,parts) VALUES(?,?,?)",
                        (i, str(i), pack([[[104, 30], [104.001, 30]]])),
                    )
            store.db.close()
            self.assertEqual(build(source, output, 2)["processed"], 2)
            self.assertEqual(build(source, output, 2)["resumed"], 2)
            self.assertEqual(select(output)["selected"], 1)
            with self.assertRaises(ValueError):
                build(source, output, 1)
            with self.assertRaises(ValueError):
                build(source, output, 2, step=10)
            with self.assertRaises(ValueError):
                build(source, source, 2)
