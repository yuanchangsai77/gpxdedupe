"""P0 safety gate regression against all existing dangerous negative fixtures.

These are deterministic synthetic controls, not real-world accuracy estimates.
Run from the repository root with project dependencies installed.
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gpx_dedupe_project'))
import route_dedupe as rd
import route_plans

spec = importlib.util.spec_from_file_location('p0_benchmark_source', ROOT / 'gpx_dedupe_project/tests/benchmark_topologies.py')
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


class P0NegativeBenchmarkTests(unittest.TestCase):
    def test_all_out_and_back_and_parallel_controls_are_not_applicable(self):
        transform = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)
        counts = {}
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'input.gpx'
            for case in benchmark.cases():
                if case.category not in ('pure_out_and_back', 'parallel_distinct_roads'):
                    continue
                with self.subTest(category=case.category, variant=case.variant, noise=case.noise_m, seed=case.seed):
                    points = case.points + [500000, 4000000]
                    coords = np.column_stack(transform.transform(points[:, 0], points[:, 1]))
                    rd.write_gpx(source, coords, np.full(len(coords), 100.), 'P0 negative control')
                    analysis = route_plans.analyze(source)
                    candidate = analysis['candidate']
                    self.assertTrue(candidate is None or candidate['blocked_reasons'], candidate)
                    self.assertEqual(analysis['preview'], analysis['original'])
                    counts[case.category] = counts.get(case.category, 0) + 1
        self.assertEqual(counts, {'pure_out_and_back': 24, 'parallel_distinct_roads': 24})


if __name__ == '__main__':
    unittest.main()
