import unittest

from route_semantics.coverage import measure
from route_semantics.coverage_fast import measure as fast
from route_semantics.density_batch import choose


class FastCoverageTests(unittest.TestCase):
    def test_equivalent(self):
        parts = [
            [[104 + i * 0.0001, 30 + (i % 7) * 0.0001] for i in range(500)],
            [[103, 30], [105, 30]],
        ]
        a, l, g = measure(parts, (100, 5000))
        b, k, h = fast(parts, (100, 5000))
        self.assertEqual(a.keys(), b.keys())
        self.assertAlmostEqual(l, k, places=5)
        self.assertAlmostEqual(g, h, places=5)
        self.assertLess(max(abs(a[v] - b[v]) for v in a), 1e-5)

    def test_rules(self):
        self.assertEqual(choose("candidate", 1000, 0, 0)[:2], (True, "rebuilt"))
        self.assertEqual(choose("candidate", 5000, 0, 1000)[:2], (True, "original"))
        self.assertFalse(choose("original", 5000, 0, 499)[0])
        self.assertTrue(choose("original", 5000, 0, 1000)[0])
        self.assertFalse(choose("original", 5000, 1, 5000)[0])

    def test_chunk_boundaries_and_negative_grids(self):
        points = [[-1 + i * .000001, -1] for i in range(4100)]
        a, length, gap = measure([points], (5000,))
        b, other, missing = fast([points], (5000,))
        self.assertEqual(a.keys(), b.keys())
        self.assertAlmostEqual(length, other, places=5)
        self.assertAlmostEqual(sum(b.values()), other, places=5)
        self.assertEqual(gap, missing)
