"""Conservative recognition contract tests, independent of legacy crop/export.

Fixtures are noiseless, use one continuous GPX segment unless explicitly testing
rejection, and make no claim about road connectivity at geometric crossings.
All writes are confined to TemporaryDirectory.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_loops import (boundary_error, circle, gpx_bytes, line,
                             source_intervals, synthetic_cases)
import loop_recognition as lr


class LoopRecognitionTests(unittest.TestCase):
    def recognize(self, points, config=None):
        raw, coords = gpx_bytes(points)
        result = lr.recognize_bytes(raw, config=config, source_name='fixture.gpx')
        self.assertIsInstance(result, dict)
        self.assertIn('report', result)
        self.assertEqual(result['schema_version'], '2.1')
        self.assert_preserved(result, coords)
        report = result['report']
        for field in ('whole_track', 'has_loops', 'loops', 'loop_candidates',
                      'closed_walks', 'completeness', 'limitations'):
            self.assertIn(field, report)
        self.assertIn(report['has_loops'], (True, False, None))
        self.assertIsInstance(report['loops'], list)
        self.assertIsInstance(report['loop_candidates'], list)
        self.assertIsInstance(report['closed_walks'], list)
        self.assertIsInstance(report['limitations'], list)
        whole = report['whole_track']
        self.assertIsInstance(whole['is_closed'], bool)
        self.assertGreaterEqual(whole['start_end_distance_m'], 0)
        self.assertGreater(whole['tolerance_m'], 0)
        self.assert_structure(report, len(coords))
        # Results must be usable by the JSON API, including exceptional numeric cases.
        json.dumps(result, allow_nan=False)
        return report

    def assert_preserved(self, result, coords):
        np.testing.assert_array_equal(np.asarray(result['original']), coords)

    def assert_structure(self, report, source_count):
        loop_ids = []
        for loop in report['loops'] + report['loop_candidates'] + report['closed_walks']:
            loop_ids.append(loop['id'])
            self.assertGreater(loop['length_m'], 0)
            if any(loop is walk for walk in report['closed_walks']):
                self.assertEqual(loop['kind'], 'closed_walk')
                self.assertIsInstance(loop['contains_retrace'], bool)
                self.assertIsInstance(loop['contains_subloops'], bool)
            else:
                self.assertTrue(loop['evidence'])
            if any(loop is confirmed for confirmed in report['loops']):
                self.assertGreaterEqual(loop['length_m'], 80)
            self.assertGreaterEqual(loop['closure_gap_m'], 0)
            self.assertIsInstance(loop['warnings'], list)
            self.assertTrue(loop['occurrences'])
            ids = []
            for occurrence in loop['occurrences']:
                ids.append(occurrence['id'])
                self.assertIn(occurrence['direction'], ('forward', 'reverse'))
                self.assertIn(occurrence['status'], ('complete', 'partial'))
                self.assertGreater(occurrence['end_m'], occurrence['start_m'])
                self.assertGreater(occurrence['length_m'], 0)
                self.assertGreaterEqual(occurrence['closure_gap_m'], 0)
                self.assertEqual(len(occurrence['source_intervals']), 1)
                self.assertEqual(occurrence['excluded_intervals'], [])
                self.assertAlmostEqual(occurrence['length_m'],
                                       occurrence['end_m']-occurrence['start_m'], delta=1e-6)
                interval = occurrence['source_intervals'][0]
                self.assertAlmostEqual(interval['start']['s_m'], occurrence['start_m'],
                                       delta=1e-6)
                self.assertAlmostEqual(interval['end']['s_m'], occurrence['end_m'],
                                       delta=1e-6)
                previous_end = -1.0
                for interval in occurrence['source_intervals']:
                    for side in ('start', 'end'):
                        boundary = interval[side]
                        self.assertEqual(boundary['segment_id'], 0)
                        self.assertGreaterEqual(boundary['edge_start_index'], 0)
                        self.assertEqual(boundary['edge_end_index'],
                                         boundary['edge_start_index']+1)
                        self.assertLess(boundary['edge_end_index'], source_count)
                        self.assertGreaterEqual(boundary['t'], 0)
                        self.assertLessEqual(boundary['t'], 1)
                        self.assertGreaterEqual(boundary['s_m'], 0)
                    start = interval['start']['s_m']
                    end = interval['end']['s_m']
                    self.assertGreaterEqual(start+1e-6, previous_end)
                    self.assertGreater(end, start)
                    previous_end = end
            self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(loop_ids), len(set(loop_ids)))

    def test_subsample_short_retrace_is_not_a_strict_simple_loop(self):
        for branch_m in (1., 10., 20.):
            with self.subTest(branch_m=branch_m):
                points = np.asarray([[0, 0], [400, 0], [400, 400],
                                     [400+branch_m, 400], [400, 400],
                                     [0, 400], [0, 0]], dtype=float)
                report = self.recognize(points)
                self.assertEqual(report['loops'], [])
                self.assertIsNot(report['has_loops'], True)
                self.assertTrue(any(w['contains_retrace'] for w in report['closed_walks']))
                self.assertTrue(report['retrace_annotations'])

    def test_sparse_right_angle_laps_are_not_noise(self):
        corners = np.asarray([[0, 0], [40, 0], [40, 40], [0, 40], [0, 0]], dtype=float)
        points = np.vstack([corners, *[corners[1:] for _ in range(5)]])
        report = self.recognize(points)
        self.assertNotIn('noise_above_supported_resolution', report['limitations'])
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(len(report['loops'][0]['occurrences']), 6)
        self.assertIs(report['has_loops'], True)

    def test_exact_short_retrace_uses_original_vertices_not_sampling_window(self):
        points = np.asarray([[0, 0], [1000, 0], [1000, 1000],
                             [1007, 1004], [1020, 1009], [1007, 1004], [1000, 1000],
                             [0, 1000], [0, 0]], dtype=float)
        raw, coords = gpx_bytes(points)
        result = lr.recognize_bytes(raw)
        report = result['report']
        self.assertEqual(result['schema_version'], '2.1')
        self.assertEqual(report['loops'], [])
        self.assertTrue(report['closed_walks'])
        self.assertEqual(len(report['retrace_annotations']), 1)
        annotation = report['retrace_annotations'][0]
        self.assertEqual([annotation[k] for k in ('start_vertex_index', 'turn_vertex_index', 'end_vertex_index')], [2, 4, 6])
        _, _, d, _ = lr._load(raw, lr.LoopRecognitionConfig())
        self.assertAlmostEqual(annotation['length_m'], d[6]-d[2], delta=1e-7)
        self.assertEqual(annotation['measurement'], 'exact_original_geometry')
        self.assertTrue(annotation['is_short_local'])
        summary = report['route_summary']
        self.assertEqual(summary['closure_kind'], 'exact_endpoint')
        self.assertAlmostEqual(summary['retrace_union_length_m'], annotation['length_m'])
        self.assertAlmostEqual(summary['retrace_union_fraction'], annotation['length_m']/d[-1])
        self.assertEqual(summary['annotation_status'], 'complete')
        self.assert_preserved(result, coords)

    def test_exact_retrace_duplicates_nested_ranges_and_separate_visits(self):
        # Collapse zero-length observations only in the analysis view. Keep raw
        # indices even when _position chooses the last equal-distance vertex.
        points = np.asarray([[0, 0], [10, 0], [10, 0], [20, 0], [10, 0], [0, 0],
                             [0, 100], [10, 100], [0, 100], [50, 200]], dtype=float)
        coords = points.copy()
        d = lr._arclength(points)
        annotations, limits = lr._exact_retraces(coords, d, lr.LoopRecognitionConfig())
        self.assertEqual(limits, [])
        self.assertEqual([(a['start_vertex_index'], a['turn_vertex_index'], a['end_vertex_index']) for a in annotations], [(0, 3, 5), (6, 7, 8)])
        summary = lr._route_summary(coords, d, 200., annotations, limits, lr.LoopRecognitionConfig())
        self.assertAlmostEqual(summary['retrace_union_length_m'], 60.)
        # A larger palindrome contains smaller palindromes: count its travel once.
        nested = np.asarray([[0, 0], [10, 0], [0, 0], [10, 0], [0, 0]], dtype=float)
        d = lr._arclength(nested)
        annotations, limits = lr._exact_retraces(nested, d, lr.LoopRecognitionConfig())
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]['length_m'], 40.)
        self.assertFalse(annotations[0]['is_short_local'])

    def test_exact_retrace_does_not_claim_near_or_unequally_sampled_geometry(self):
        for points in ([[0, 0], [10, 0], [20, 0], [10, .01], [0, .01]],
                       [[0, 0], [10, 0], [20, 0], [15, 0], [0, 0]]):
            coords = np.asarray(points, dtype=float)
            annotations, limits = lr._exact_retraces(coords, lr._arclength(coords), lr.LoopRecognitionConfig())
            self.assertEqual(annotations, [])
            self.assertEqual(limits, [])

    def test_annotation_budgets_are_separate_from_strict_completeness(self):
        coords = np.asarray([[0, 0], [10, 0], [20, 0], [10, 0], [0, 0]], dtype=float)
        d = lr._arclength(coords)
        annotations, limits = lr._exact_retraces(coords, d, lr.LoopRecognitionConfig(max_spatial_pairs=1))
        self.assertEqual(annotations, [])
        self.assertIn('retrace_comparison_limit', limits)
        summary = lr._route_summary(coords, d, 0., annotations, limits, lr.LoopRecognitionConfig())
        self.assertEqual(summary['annotation_status'], 'partial')
        repeated = np.asarray([[0, 0], [10, 0], [0, 0], [0, 100], [10, 100], [0, 100], [50, 200]], dtype=float)
        annotations, limits = lr._exact_retraces(repeated, lr._arclength(repeated), lr.LoopRecognitionConfig(max_occurrences=1))
        self.assertEqual(len(annotations), 1)
        self.assertIn('retrace_annotation_limit', limits)

    def test_whole_outback_annotation_is_not_short_or_simple_loop(self):
        outward = np.asarray([[0, 0], [90, 20], [220, 120], [300, 80]], dtype=float)
        report = self.recognize(np.vstack([outward, outward[-2::-1]]))
        annotation = report['retrace_annotations'][0]
        self.assertAlmostEqual(annotation['fraction_of_track'], 1.)
        self.assertFalse(annotation['is_short_local'])
        self.assertEqual(report['loops'], [])
        self.assertEqual(report['route_summary']['closure_kind'], 'exact_endpoint')

    def test_optional_real_short_retrace_34044351(self):
        path = Path(r'D:\7281\workspace\route\1\komoot\Algeria\Algeria\02_明细\gpx\34044351.gpx')
        if not path.is_file():
            self.skipTest('Private 34044351.gpx fixture unavailable')
        raw = path.read_bytes()
        report = lr.recognize_bytes(raw)['report']
        self.assertEqual(report['input_points'], 143)
        self.assertAlmostEqual(report['input_length_m'], 4256.602, delta=.02)
        self.assertEqual(len(report['retrace_annotations']), 1)
        annotation = report['retrace_annotations'][0]
        self.assertEqual([annotation[k] for k in ('start_vertex_index', 'turn_vertex_index', 'end_vertex_index')], [80, 82, 84])
        self.assertAlmostEqual(annotation['length_m'], 44.42, delta=.02)
        self.assertTrue(annotation['is_short_local'])
        self.assertAlmostEqual(annotation['fraction_of_track'], .0104, delta=.0001)
        self.assertEqual(report['loops'], [])
        self.assertEqual(path.read_bytes(), raw)

    def test_annotation_layer_never_changes_strict_decomposition(self):
        points = np.asarray([[0, 0], [1000, 0], [1000, 1000], [1020, 1000],
                             [1000, 1000], [0, 1000], [0, 0]], dtype=float)
        raw, _ = gpx_bytes(points)
        measured = lr.recognize_bytes(raw)['report']
        with patch.object(lr, '_exact_retraces', return_value=([], ['retrace_comparison_limit'])):
            unmeasured = lr.recognize_bytes(raw)['report']
        for field in ('loops', 'loop_candidates', 'closed_walks', 'has_loops',
                      'completeness', 'limitations', 'events', 'relations'):
            self.assertEqual(measured[field], unmeasured[field], field)
        self.assertEqual(unmeasured['route_summary']['annotation_status'], 'partial')
        # Independent annotation failure cannot silently downgrade a successful
        # strict decomposition, or invent a successful absence of retracing.
        self.assertEqual(unmeasured['completeness'], 'complete')

    def test_summary_uses_union_and_distinguishes_tolerance_from_exact_closure(self):
        coords = np.asarray([[0, 0], [20, 0], [40, 0], [60, 0], [80, 0], [100, 0]], dtype=float)
        d = lr._arclength(coords)
        annotations = [{'id': 'a', 'source_interval': {'start': lr._position(0., d), 'end': lr._position(60., d)}},
                       {'id': 'b', 'source_interval': {'start': lr._position(40., d), 'end': lr._position(80., d)}}]
        summary = lr._route_summary(coords, d, 10., annotations, [], lr.LoopRecognitionConfig())
        self.assertEqual(summary['closure_kind'], 'within_tolerance')
        self.assertEqual(summary['retrace_union_length_m'], 80.)
        self.assertEqual(summary['retrace_union_fraction'], .8)
        self.assertEqual(lr._route_summary(coords, d, 100., [], [], lr.LoopRecognitionConfig())['closure_kind'], 'open')
        near = np.asarray([[0, 0], [400, 0], [400, 400], [0, 400], [2, 0]], dtype=float)
        report = self.recognize(near)
        self.assertEqual(report['route_summary']['closure_kind'], 'within_tolerance')
        self.assertEqual(report['retrace_annotations'], [])

    def test_defaults_are_explicit_dataclass(self):
        cfg = lr.LoopRecognitionConfig()
        self.assertTrue(dataclasses.is_dataclass(cfg))
        self.assertEqual(cfg.spacing_m, 8)
        self.assertEqual(cfg.match_radius_m, 12)
        self.assertEqual(cfg.closure_radius_m, 25)
        self.assertEqual(cfg.min_loop_length_m, 80)
        for field in ('max_samples', 'max_spatial_pairs', 'max_crossing_pairs',
                      'max_loops', 'max_occurrences'):
            self.assertGreater(getattr(cfg, field), 0)

    def test_single_circle(self):
        report = self.recognize(circle())
        self.assertTrue(report['whole_track']['is_closed'])
        self.assertIs(report['has_loops'], True)
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(len(report['loops'][0]['occurrences']), 1)
        self.assertAlmostEqual(report['loops'][0]['length_m'], 2*np.pi*160, delta=25)

    def test_two_through_six_laps_are_one_loop_with_separate_occurrences(self):
        for count in range(2, 7):
            with self.subTest(laps=count):
                report = self.recognize(circle(laps=count))
                self.assertIs(report['has_loops'], True)
                self.assertEqual(len(report['loops']), 1)
                self.assertEqual(report['loop_candidates'], [])
                loop = report['loops'][0]
                self.assertEqual(len(loop['occurrences']), count)
                self.assertEqual({occ['direction'] for occ in loop['occurrences']},
                                 {'forward'})
                self.assertAlmostEqual(loop['length_m'], 2*np.pi*160, delta=25)
                starts = [occ['start_m'] for occ in loop['occurrences']]
                self.assertEqual(starts, sorted(starts))
                for previous, current in zip(loop['occurrences'], loop['occurrences'][1:]):
                    self.assertAlmostEqual(previous['end_m'], current['start_m'], delta=25)

    def test_open_route(self):
        report = self.recognize(line([[0, 0], [400, 0], [400, 400], [0, 400]]))
        self.assertFalse(report['whole_track']['is_closed'])
        self.assertIs(report['has_loops'], False)
        self.assertEqual(report['loops'] + report['loop_candidates'], [])

    def test_straight_and_curved_outbacks_are_not_loops(self):
        outward = line([[0, 0], [200, 0], [300, 180], [600, 250]])
        for points in (line([[0, 0], [600, 0], [0, 0]]),
                       np.vstack([outward, outward[-2::-1]])):
            with self.subTest(points=len(points)):
                report = self.recognize(points)
                self.assertTrue(report['whole_track']['is_closed'])
                self.assertIs(report['has_loops'], False)
                self.assertEqual(report['loops'] + report['loop_candidates'], [])

    def test_closed_and_open_lollipops_locate_only_loop(self):
        for case in synthetic_cases():
            if case.category not in ('closed_lollipop', 'open_lollipop'):
                continue
            with self.subTest(category=case.category):
                report = self.recognize(case.points)
                self.assertEqual(report['whole_track']['is_closed'], case.expected_closed)
                self.assertIs(report['has_loops'], True)
                self.assertEqual(len(report['loops']), 1)
                self.assertEqual(report['loop_candidates'], [])
                loop = report['loops'][0]
                self.assertEqual(len(loop['occurrences']), 1)
                error = boundary_error(loop['occurrences'][0], case.expected_intervals)
                self.assertIsNotNone(error)
                self.assertLessEqual(error, 25)
                self.assertAlmostEqual(loop['length_m'], 1200, delta=35)

    def test_figure_eight_counts_geometric_candidates_without_connectivity_claim(self):
        case = next(case for case in synthetic_cases() if case.category == 'figure_eight')
        report = self.recognize(case.points)
        self.assertTrue(report['whole_track']['is_closed'])
        self.assertEqual(len(report['loops']) + len(report['loop_candidates']), 2)
        self.assertTrue(report['limitations'])
        diagnostics = report['limitations'] + [warning
            for loop in report['loops'] + report['loop_candidates']
            for warning in loop['warnings']]
        self.assertTrue(any('connectivity' in str(item).lower() for item in diagnostics))
        # Candidate evidence is not proof that crossing roads actually connect.
        if report['loop_candidates']:
            self.assertIsNot(report['has_loops'], False)

    def assert_closed_walk_range(self, report, start, end, *, retrace=None, subloops=None):
        matches = [(walk, occ) for walk in report['closed_walks']
                   for occ in walk['occurrences']
                   if abs(occ['start_m']-start) <= 25 and abs(occ['end_m']-end) <= 25]
        self.assertTrue(matches, f'No continuous closed walk spans {start}..{end} m')
        walk, occurrence = matches[0]
        self.assertEqual(occurrence['status'], 'complete')
        self.assertAlmostEqual(occurrence['length_m'], end-start, delta=35)
        self.assertLessEqual(boundary_error(occurrence, ((start, end),)), 25)
        if retrace is not None:
            self.assertIs(walk['contains_retrace'], retrace)
        if subloops is not None:
            self.assertIs(walk['contains_subloops'], subloops)
        return walk, occurrence

    def test_internal_branch_is_full_closed_walk_not_synthetic_simple_loop(self):
        for category in ('internal_branch', 'reverse_internal_branch',
                         'nested_branches', 'repeated_branches'):
            case = next(case for case in synthetic_cases() if case.category == category)
            with self.subTest(category=category):
                report = self.recognize(case.points)
                self.assertTrue(report['whole_track']['is_closed'])
                self.assertEqual(report['loops'] + report['loop_candidates'], [])
                self.assertIs(report['has_loops'], False)
                start, end = case.expected_closed_walk_intervals[0]
                self.assert_closed_walk_range(report, start, end, retrace=True)
                # No excision of CD/DC may fabricate an ABCEA 1600 m loop.
                self.assertFalse(any(abs(loop['length_m']-1600) < 35
                                     for loop in report['loops']))

    def test_internal_subloop_does_not_fabricate_a_simple_outer_loop(self):
        case = next(case for case in synthetic_cases() if case.category == 'internal_subloop')
        report = self.recognize(case.points)
        self.assertIs(report['has_loops'], True)
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(report['loop_candidates'], [])
        loop = report['loops'][0]
        self.assertAlmostEqual(loop['length_m'], 800, delta=35)
        self.assertEqual(len(loop['occurrences']), 1)
        self.assertLessEqual(boundary_error(loop['occurrences'][0], ((400, 1200),)), 25)
        self.assert_closed_walk_range(report, 0, 2400, retrace=False, subloops=True)

    def test_subsequent_full_loop_is_not_polluted_by_prior_branch_walk(self):
        case = next(case for case in synthetic_cases()
                    if case.category == 'branch_then_clean_loop')
        report = self.recognize(case.points)
        self.assertIs(report['has_loops'], True)
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(report['loop_candidates'], [])
        loop = report['loops'][0]
        self.assertAlmostEqual(loop['length_m'], 1600, delta=35)
        complete = [occ for occ in loop['occurrences'] if occ['status'] == 'complete']
        self.assertEqual(len(complete), 1)
        self.assertLessEqual(boundary_error(complete[0], ((2100, 3700),)), 25)
        # Matching fragments from the polluted traversal must stay independent;
        # they must never become a second complete occurrence by skipping CD/DC.
        for occurrence in loop['occurrences']:
            start, end = source_intervals(occurrence)[0]
            self.assertFalse(start < 410 and end > 890 and start < 2100)
        self.assert_closed_walk_range(report, 0, 2100, retrace=True)

    def test_reverse_repeated_loop_groups_opposite_directions(self):
        forward = circle()
        report = self.recognize(np.vstack([forward, forward[-2::-1]]))
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(report['loop_candidates'], [])
        occurrences = report['loops'][0]['occurrences']
        self.assertEqual(len(occurrences), 2)
        self.assertEqual({occ['direction'] for occ in occurrences}, {'forward', 'reverse'})

    def test_strong_jitter_outback_is_never_a_confirmed_loop(self):
        # Safety regression only, not a promise of useful recognition at sigma8.
        # Independent endpoint jitter and spacing match the legacy synthetic style.
        ideal = line([[0, 0], [3000, 0], [0, 0]])
        noisy = ideal + np.random.default_rng(1729).normal(0, 8, ideal.shape)
        report = self.recognize(noisy)
        self.assertEqual(report['loops'], [])
        self.assertIsNot(report['has_loops'], True)
        if report['loop_candidates']:
            self.assertIsNone(report['has_loops'])
            self.assertTrue(report['limitations'])

    def test_trailing_half_lap_is_partial_occurrence_of_known_loop(self):
        full = circle()
        half = full[:(len(full)+1)//2]
        points = np.vstack([full, half[1:]])
        report = self.recognize(points)
        self.assertFalse(report['whole_track']['is_closed'])
        self.assertIs(report['has_loops'], True)
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(report['loop_candidates'], [])
        occurrences = report['loops'][0]['occurrences']
        self.assertEqual([occ['status'] for occ in occurrences], ['complete', 'partial'])
        first, fragment = occurrences
        self.assertEqual(fragment['direction'], first['direction'])
        self.assertAlmostEqual(fragment['start_m'], first['end_m'], delta=25)
        self.assertAlmostEqual(fragment['length_m'], first['length_m']/2, delta=25)
        self.assertGreater(fragment['closure_gap_m'], 100)
        self.assertEqual(fragment['excluded_intervals'], [])

    def test_partial_laps_survive_access_exit_and_near_full_progress(self):
        full = circle()
        fixtures = {
            'access': np.vstack([line([[460, 0], [160, 0]])[:-1], full, full[1:64]]),
            'exit': np.vstack([full, full[1:64], line([full[63], full[63]+[300, 0]])[1:]]),
            'near_full': np.vstack([full, full[1:121]]),
        }
        for name, points in fixtures.items():
            with self.subTest(name=name):
                report = self.recognize(points)
                self.assertEqual(len(report['loops']), 1)
                occurrences = report['loops'][0]['occurrences']
                self.assertEqual([o['status'] for o in occurrences], ['complete', 'partial'])
                partial = occurrences[1]
                expected_length = 950 if name == 'near_full' else 500
                self.assertAlmostEqual(partial['length_m'], expected_length, delta=30)
                self.assertLess(partial['length_m'], occurrences[0]['length_m'])

    def test_branch_splits_partial_lap_into_independent_continuous_occurrences(self):
        full = circle()
        points = np.vstack([full, full[1:32],
                            line([full[31], full[31]*2, full[31]])[1:], full[32:64]])
        report = self.recognize(points)
        self.assertEqual(len(report['loops']), 1)
        self.assertEqual(report['loop_candidates'], [])
        occurrences = report['loops'][0]['occurrences']
        self.assertEqual([o['status'] for o in occurrences],
                         ['complete', 'partial', 'partial'])
        complete, before, after = occurrences
        self.assertAlmostEqual(before['length_m'], 245, delta=25)
        self.assertAlmostEqual(after['length_m'], 255, delta=25)
        self.assertAlmostEqual(before['start_m'], complete['end_m'], delta=25)
        self.assertAlmostEqual(after['start_m']-before['end_m'], 320, delta=25)
        for partial in (before, after):
            self.assertEqual(partial['direction'], complete['direction'])
            self.assertEqual(len(partial['source_intervals']), 1)
            self.assertEqual(partial['excluded_intervals'], [])
            self.assertLess(partial['length_m'], 300)

    def test_combined_search_limits_preserve_both_diagnostics(self):
        raw, _ = gpx_bytes(circle(laps=3))
        config = lr.LoopRecognitionConfig(max_crossing_pairs=1, max_spatial_pairs=1)
        report = lr.recognize_bytes(raw, config)['report']
        self.assertEqual(report['completeness'], 'partial')
        self.assertIsNone(report['has_loops'])
        self.assertIn('crossing_pair_limit', report['limitations'])
        self.assertIn('spatial_pair_limit', report['limitations'])

    def test_bytes_and_path_leave_input_unchanged_and_write_nothing(self):
        raw, coords = gpx_bytes(circle())
        snapshot = bytes(raw)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'original.gpx'
            source.write_bytes(raw)
            before = set(Path(directory).iterdir())
            by_path = lr.recognize_loops(source)
            by_bytes = lr.recognize_bytes(raw, source_name=source.name)
            self.assertEqual(source.read_bytes(), snapshot)
            self.assertEqual(raw, snapshot)
            self.assertEqual(set(Path(directory).iterdir()), before)
            self.assert_preserved(by_path, coords)
            self.assert_preserved(by_bytes, coords)
            self.assertEqual(by_path['report'], by_bytes['report'])

    def test_does_not_call_legacy_run_or_plan_workflow(self):
        raw, _ = gpx_bytes(circle())
        # Patch both public entrypoints and direct aliases, if any. Low-level
        # projection helpers remain reusable; mutation workflows must not run.
        rd = importlib.import_module('route_dedupe')
        plans = importlib.import_module('route_plans')
        blocked = (rd.run, plans.analyze, plans.apply)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for module, name in ((rd, 'run'), (plans, 'analyze'), (plans, 'apply')):
                stack.enter_context(patch.object(module, name,
                    side_effect=AssertionError('mutation workflow called')))
            for name, value in list(vars(lr).items()):
                if any(value is function for function in blocked):
                    stack.enter_context(patch.object(lr, name,
                        side_effect=AssertionError('mutation workflow alias called')))
            result = lr.recognize_bytes(raw)
            self.assertTrue(result['report']['loops'])

    def test_invalid_xml_and_nonfinite_coordinates(self):
        valid, _ = gpx_bytes(circle())
        latitude = valid.split(b'lat="', 1)[1].split(b'"', 1)[0]
        longitude = valid.split(b'lon="', 1)[1].split(b'"', 1)[0]
        invalid = [b'not xml', b'<gpx><trk>', b'<gpx/>',
                   valid.replace(b'lat="'+latitude+b'"', b'lat="nan"', 1),
                   valid.replace(b'lon="'+longitude+b'"', b'lon="inf"', 1),
                   valid.replace(b'<ele>100</ele>', b'<ele>nan</ele>', 1)]
        for raw in invalid:
            with self.subTest(prefix=raw[:60]):
                with self.assertRaises(lr.RecognitionError):
                    lr.recognize_bytes(raw)

    def test_multisegment_is_rejected_without_joining_discontinuities(self):
        raw, _ = gpx_bytes(circle())
        split = raw.replace(b'</trkseg>', b'</trkseg><trkseg>'
                            b'<trkpt lat="36" lon="9"/>'
                            b'<trkpt lat="36.01" lon="9.01"/></trkseg>', 1)
        with self.assertRaises(lr.RecognitionError):
            lr.recognize_bytes(split)

    def test_oversized_input_is_rejected_before_parsing(self):
        cfg = lr.LoopRecognitionConfig()
        self.assertEqual(cfg.max_input_bytes, 16*1024*1024)
        raw = b' '*(cfg.max_input_bytes+1)
        with self.assertRaises(lr.RecognitionError):
            lr.recognize_bytes(raw)
        valid, _ = gpx_bytes(circle())
        with self.assertRaises(lr.RecognitionError):
            lr.recognize_bytes(valid, config=lr.LoopRecognitionConfig(max_input_bytes=32))

    def test_config_rejects_invalid_numeric_values(self):
        raw, _ = gpx_bytes(circle())
        for field in ('spacing_m', 'match_radius_m', 'closure_radius_m',
                      'min_loop_length_m', 'max_samples', 'max_spatial_pairs',
                      'max_crossing_pairs', 'max_loops', 'max_occurrences'):
            for value in (0, -1, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(lr.RecognitionError):
                        cfg = lr.LoopRecognitionConfig(**{field: value})
                        lr.recognize_bytes(raw, config=cfg)

    def test_sample_admission_limit_raises_recognition_error(self):
        raw, _ = gpx_bytes(circle())
        with self.assertRaises(lr.RecognitionError):
            lr.recognize_bytes(raw, config=lr.LoopRecognitionConfig(max_samples=1))

    def test_search_budget_exhaustion_returns_partial_with_diagnostics(self):
        figure8 = next(case.points for case in synthetic_cases()
                       if case.category == 'figure_eight')
        fixtures = {'max_spatial_pairs': circle(laps=3),
                    'max_crossing_pairs': circle(laps=3),
                    'max_loops': figure8,
                    'max_occurrences': circle(laps=3)}
        for field, points in fixtures.items():
            with self.subTest(budget=field):
                raw, _ = gpx_bytes(points)
                report = lr.recognize_bytes(raw,
                    config=lr.LoopRecognitionConfig(**{field: 1}))['report']
                self.assertEqual(report['completeness'], 'partial')
                self.assertTrue(report['limitations'])
                if report['loops']:
                    self.assertIs(report['has_loops'], True)
                else:
                    self.assertIsNone(report['has_loops'])
                if field == 'max_loops':
                    self.assertLessEqual(len(report['loops'])+len(report['loop_candidates']), 1)
                if field == 'max_occurrences':
                    self.assertLessEqual(sum(len(loop['occurrences'])
                        for loop in report['loops']+report['loop_candidates']), 1)

    def test_deterministic_results_and_ids(self):
        raw, _ = gpx_bytes(circle(laps=3))
        first = lr.recognize_bytes(raw)
        second = lr.recognize_bytes(raw)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
