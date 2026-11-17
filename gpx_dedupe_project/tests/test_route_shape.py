"""Self-contained whole-route visual shape tests, distinct from strict loops."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import loop_recognition as lr
from benchmark_loops import circle, gpx_bytes, line


def square(side=400.):
    return np.asarray([[0, 0], [side, 0], [side, side], [0, side], [0, 0]], dtype=float)


class RouteShapeTests(unittest.TestCase):
    def classify(self, points, config=None):
        raw, _ = gpx_bytes(points)
        before = hashlib.sha256(raw).hexdigest()
        result = lr.classify_route_bytes(raw, config)
        self.assertEqual(result['kind'], 'route_shape_classification')
        self.assertIn(result['category'], (1, 2, 3, 4))
        self.assertEqual(result['status'], 'failed' if result['category'] == 1 else 'classified')
        self.assertEqual(hashlib.sha256(raw).hexdigest(), before)
        self.assertNotIn('original', result)
        json.dumps(result, allow_nan=False)
        return result

    def test_single_irregular_and_narrow_resolved_rings(self):
        for points in (square(), circle(),
                       np.asarray([[0, 0], [600, 0], [580, 160], [100, 400], [-80, 70], [0, 0]]),
                       np.asarray([[0, 0], [1000, 0], [1000, 80], [0, 80], [0, 0]])):
            with self.subTest(points=len(points)):
                self.assertEqual(self.classify(points)['category'], 2)

    def test_q_stem_and_short_retrace_are_accepted(self):
        stem = np.asarray([[-150, 0], [0, 0]], dtype=float)
        q = np.vstack([stem[:-1], square(), stem[-2::-1]])
        self.assertEqual(self.classify(q)['category'], 2)
        branch = np.asarray([[0, 0], [400, 0], [400, 400], [410, 400],
                             [400, 400], [0, 400], [0, 0]], dtype=float)
        result = self.classify(branch)
        self.assertEqual(result['category'], 2)
        strict = lr.recognize_bytes(gpx_bytes(branch)[0])['report']
        self.assertEqual(strict['loops'], [])
        self.assertTrue(strict['closed_walks'])

    def test_long_stem_small_ring_is_not_ring_dominant(self):
        stem = np.asarray([[-5000, 0], [0, 0]], dtype=float)
        points = np.vstack([stem[:-1], square(100), stem[-2::-1]])
        self.assertEqual(self.classify(points)['category'], 3)

    def test_open_and_straight_or_curved_pure_outbacks(self):
        outward = np.asarray([[0, 0], [300, 100], [700, 450]], dtype=float)
        for points in (outward, np.vstack([outward, outward[-2::-1]]),
                       np.asarray([[0, 0], [1000, 0], [0, 0]])):
            self.assertEqual(self.classify(points)['category'], 3)

    def test_comparable_two_lobes_are_nonstandard(self):
        first = square()
        second = np.asarray([[0, 0], [-400, 0], [-400, -400], [0, -400], [0, 0]], dtype=float)
        result = self.classify(np.vstack([first, second[1:]]))
        self.assertEqual(result['category'], 4)
        self.assertEqual(result['metrics']['significant_ring_blocks'], 2)

    def test_internal_dead_end_is_not_a_q_stem(self):
        # A long inward branch is structurally different from a short exterior
        # access leg even when both graph components are technically bridges.
        points = np.asarray([[0, 0], [400, 0], [400, 400], [160, 160],
                             [400, 400], [0, 400], [0, 0]], dtype=float)
        self.assertEqual(self.classify(points)['category'], 4)

    def test_narrow_corridor_fails_but_resolved_crossed_lobes_are_nonstandard(self):
        narrow = np.asarray([[0, 0], [1000, 0], [1000, 5], [0, 5], [0, 0]], dtype=float)
        result = self.classify(narrow)
        self.assertEqual(result['category'], 1)
        self.assertEqual(result['reason_code'], 'ambiguous_shape')
        crossing = np.asarray([[0, 0], [400, 400], [0, 400], [400, 0], [0, 0]], dtype=float)
        result = self.classify(crossing)
        self.assertEqual(result['category'], 4)
        self.assertTrue(result['metrics']['visual_crossing_hypothesis'])

    def test_substantial_internal_chord_is_nonstandard(self):
        # Outer square then an observed diagonal, then return along the outside.
        points = np.vstack([square(), [[400, 400], [400, 0], [0, 0]]])
        result = self.classify(points)
        self.assertEqual(result['category'], 4)
        self.assertGreater(result['metrics']['main_internal_share'], .12)

    def test_geometry_transform_direction_and_sampling_invariance(self):
        original = square()
        angle = .73
        rotation = np.asarray([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        variants = [original, original[::-1], original @ rotation + [3000, -4000],
                    np.vstack([original[2:-1], original[:3]]), line(original, step=13.),
                    np.repeat(original, 2, axis=0)]
        for variant in variants:
            self.assertEqual(self.classify(variant)['category'], 2)

    def test_repeated_laps_do_not_inflate_visual_geometry(self):
        first = square()
        twice = np.vstack([first, first[1:]])
        one = self.classify(first)
        two = self.classify(twice)
        self.assertEqual(two['category'], 2)
        self.assertAlmostEqual(one['metrics']['unique_length_m'], two['metrics']['unique_length_m'], delta=.01)
        self.assertAlmostEqual(two['metrics']['travel_length_m'], 2*one['metrics']['travel_length_m'], delta=.01)

    def test_straight_segment_different_sampling_densities_not_a_fake_ring(self):
        points = np.asarray([[0, 0], [500, 0], [1000, 0], [750, 0], [0, 0]], dtype=float)
        self.assertEqual(self.classify(points)['category'], 3)

    def test_review_butterfly_class_is_independent_of_sampling(self):
        points = np.asarray([[0, 0], [800, 400], [0, 400], [400, 0], [0, 0]], dtype=float)
        results = [self.classify(variant) for variant in (points, line(points, step=8),
                                                         line(points, step=17), points[::-1])]
        self.assertEqual([r['category'] for r in results], [4] * len(results))

    def test_review_narrow_triangle_is_ambiguous_at_all_densities(self):
        points = np.asarray([[0, 0], [1000, 0], [0, 5], [0, 0]], dtype=float)
        for variant in (points, line(points, step=8), line(points, step=17), points[::-1]):
            with self.subTest(points=len(variant)):
                result = self.classify(variant)
                self.assertEqual(result['category'], 1)
                self.assertEqual(result['reason_code'], 'ambiguous_shape')

    def test_review_one_metre_endpoint_gap_preserves_visual_ring(self):
        points = square()
        points[-1] = [0, 1]
        raw, _ = gpx_bytes(points)
        for variant in (points, points[::-1], line(points, step=8)):
            result = self.classify(variant)
            self.assertEqual(result['category'], 2)
            self.assertTrue(result['metrics']['near_closure_assumed'])
        # The visual closure hypothesis must not change strict diagnostics.
        self.assertFalse(lr.recognize_bytes(raw)['report']['whole_track']['start_end_distance_m'] == 0)

    def test_review_single_q_stem_cannot_invent_nonstandard_structure(self):
        for stem in (150., 1200., 5000.):
            points = np.vstack([[[-stem, 0]], square(), [[-stem, 0]]])
            result = self.classify(points)
            self.assertIn(result['category'], (1, 2, 3))
            self.assertNotEqual(result['category'], 4)
            if stem == 150:
                self.assertEqual(result['category'], 2)
            elif stem == 5000:
                self.assertEqual(result['category'], 3)

    def test_near_closure_is_conservative_for_reverse_arms(self):
        points = np.asarray([[0, 0], [500, 0], [0, 1]], dtype=float)
        result = self.classify(points)
        self.assertEqual(result['category'], 1)
        self.assertFalse(result['metrics'].get('near_closure_assumed', False))

    def test_review_physical_evidence_is_stable_under_collinear_sampling(self):
        triangle = np.asarray([[0, 0], [1000, 0], [0, 5], [0, 0]], dtype=float)
        butterfly = np.asarray([[0, 0], [800, 400], [0, 400], [400, 0], [0, 0]], dtype=float)
        angle = .41
        rotation = np.asarray([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        for points, category in ((triangle, 1), (butterfly, 4)):
            results = [self.classify(variant) for variant in (
                points, line(points, step=8), line(points, step=17),
                line(points @ rotation + [1000, -1500], step=13))]
            self.assertEqual([r['category'] for r in results], [category] * len(results))
            metric = 'near_corridor_share' if category == 1 else 'unresolved_crossing_share'
            # Rigid transforms can move an interpolated crossing within the
            # numerical vertex-on-edge tolerance of an existing sample. Its
            # observed-vs-hypothesized status may then differ; the class must
            # not. Compare physical diagnostic magnitude on identical geometry.
            for result in results[1:3]:
                self.assertAlmostEqual(result['metrics'][metric], results[0]['metrics'][metric], delta=.005)

    def test_second_review_repeated_near_endpoints_are_direction_invariant(self):
        points = np.asarray([[0,0],[400,0],[400,400],[0,400],[0,1],
                             [0,400],[400,400],[400,0],[0,0],
                             [400,0],[400,400],[0,400],[0,1]], dtype=float)
        results = [self.classify(points), self.classify(points[::-1])]
        self.assertEqual([r['category'] for r in results], [1, 1])
        self.assertTrue(all(not r['metrics'].get('near_closure_assumed', False) for r in results))

    def test_second_review_one_sided_crossing_sample_is_not_observed_junction(self):
        points = np.asarray([[0,0],[500,500],[0,500],[25,0],[0,0]], dtype=float)
        results = [self.classify(p) for p in (points, line(points, step=8),
                                               line(points, step=17), points[::-1])]
        self.assertTrue(all(r['category'] != 2 for r in results))
        self.assertEqual(len({r['category'] for r in results}), 1)
        self.assertTrue(all(r['metrics'].get('visual_crossing_hypothesis') for r in results))

    def test_second_review_crossing_neighborhood_survives_change_of_start(self):
        points = np.asarray([[0,0],[800,800],[0,800],[40,0],[0,0]], dtype=float)
        dense = line(points, step=50)
        results = []
        for offset in (0, 1, 7, len(dense)//2):
            body = np.roll(dense[:-1], -offset, axis=0)
            results.append(self.classify(np.vstack([body, body[:1]])))
        self.assertEqual(len({r['category'] for r in results}), 1)
        self.assertNotEqual(results[0]['category'], 2)
        for r in results[1:]:
            self.assertAlmostEqual(r['metrics']['unresolved_crossing_share'],
                                   results[0]['metrics']['unresolved_crossing_share'], delta=.002)

    def test_second_review_partial_narrow_corridor_is_measured(self):
        for width in (13., 20.):
            points = np.asarray([[0,0],[1000,0],[0,width],[0,0]], dtype=float)
            for p in (points, points[::-1], line(points, step=8)):
                result = self.classify(p)
                self.assertEqual(result['category'], 1)
                self.assertGreater(result['metrics']['near_corridor_share'], .2)

    def test_second_review_visual_only_ring_cannot_become_oq_by_scaling(self):
        points = np.asarray([[0,0],[1000,1000],[0,1000],[1000,0]], dtype=float)
        for scale in (.4, 1., 2.):
            for p in (points * scale, line(points * scale, step=17)):
                result = self.classify(p)
                self.assertEqual(result['category'], 1)
                self.assertTrue(result['metrics']['visual_crossing_hypothesis'])
                self.assertFalse(result['metrics'].get('independent_ring_evidence', False))

    def test_final_review_corner_on_another_edge_is_not_an_oq_junction(self):
        fixtures = [np.asarray([[0,0],[400,0],[400,400],[0,400],[200,0],[300,-100],[0,0]], float),
                    np.asarray([[0,0],[400,0],[400,400],[0,400],[200,0],[0,200],[0,0]], float)]
        for points in fixtures:
            results = [self.classify(p) for p in (points, points[::-1], line(points, step=8))]
            self.assertNotIn(2, [r['category'] for r in results])
            self.assertEqual(len({r['category'] for r in results}), 1)

    def test_final_review_extra_branch_cannot_disable_crossing_evidence(self):
        points = np.asarray([[0,0],[500,500],[1000,1000],[0,1000],
                             [500,500],[1000,0]], float)
        branch = np.asarray([[0,0],[500,500],[1000,1000],[0,1000],
                             [500,500],[501,500],[500,500],[1000,0]], float)
        for p in (points, branch, branch[::-1], line(branch, step=8)):
            result = self.classify(p)
            self.assertNotEqual(result['category'], 2)
            self.assertTrue(result['metrics']['visual_crossing_hypothesis'])
            self.assertFalse(result['metrics']['independent_ring_evidence'])

    def test_final_review_collapsed_microcycles_are_structured_failure(self):
        for size in (.01, .02):
            points = np.vstack([np.tile([[0,0],[size,0],[0,size]], (3000,1)), [[0,0]]])
            result = self.classify(points)
            self.assertEqual(result['category'], 1)
            self.assertEqual(result['reason_code'], 'ambiguous_shape')

    def test_configuration_validation_and_resource_failures(self):
        raw, _ = gpx_bytes(square())
        for config in ({'unknown': 1}, {'max_points': 0}, {'dominant_ring_share': float('nan')},
                       {'vertex_on_edge_tolerance_m': 12}, {'max_candidate_pairs': True}):
            with self.assertRaises(lr.RecognitionError):
                lr.classify_route_bytes(raw, config)
        for config in (lr.RouteShapeConfig(max_candidate_pairs=1),
                       lr.RouteShapeConfig(max_graph_edges=1), lr.RouteShapeConfig(max_face_steps=1)):
            result = lr.classify_route_bytes(raw, config)
            self.assertEqual(result['category'], 1)
            self.assertEqual(result['reason_code'], 'resource_limit')

    def test_bad_input_and_short_geometry_are_structured_failures(self):
        for raw in (b'bad xml', b'<gpx/>', b'<!DOCTYPE gpx><gpx/>', b'x'*(16*1024*1024+1)):
            result = lr.classify_route_bytes(raw)
            self.assertEqual(result['category'], 1)
            self.assertIn('reason_code', result)
        self.assertEqual(self.classify(square(5))['category'], 1)

    def test_missing_file_failure_is_private_and_file_api_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'private.gpx'
            result = lr.classify_route(path)
            self.assertEqual(result['category'], 1)
            self.assertNotIn(directory, json.dumps(result))
            raw, _ = gpx_bytes(square())
            path.write_bytes(raw)
            before = set(Path(directory).iterdir())
            self.assertEqual(lr.classify_route(path), lr.classify_route_bytes(raw))
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(set(Path(directory).iterdir()), before)

    def test_label_manifest_is_not_runtime_input(self):
        spec = json.loads(Path(__file__).with_name('route_shape_labels.json').read_text())
        self.assertEqual(len(spec['labels']), 52)
        self.assertEqual(sum(label == 2 for label in spec['labels'].values()), 38)
        self.assertEqual(sum(label == 3 for label in spec['labels'].values()), 8)
        self.assertEqual(sum(label == 4 for label in spec['labels'].values()), 6)
        self.assertTrue(set(spec['calibration_excluded']).issubset(spec['labels']))
        self.assertFalse(set(spec['labels']) & set(spec['unlabeled']))


if __name__ == '__main__':
    unittest.main()
