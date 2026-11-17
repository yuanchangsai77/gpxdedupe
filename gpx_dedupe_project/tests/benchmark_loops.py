#!/usr/bin/env python3
"""Small, deterministic recognition benchmark; not real-road accuracy.

Run with the project's Python environment. JSON is printed to stdout; no files
are written. Unknown/incomplete results and execution errors are separate from
positive/negative predictions. No legacy transformation benchmark is imported or
changed. Geometry is exact and noiseless: these scores make no noise claim.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from time import perf_counter

import numpy as np
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TO_WGS84 = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)


@dataclass
class Case:
    category: str
    variant: str
    points: np.ndarray
    expected_loops: int
    expected_occurrences: int  # Complete simple-loop occurrences only.
    expected_closed: bool
    # Known source-arclength intervals for one occurrence; metre tolerances
    # account for the recognizer's resampling and local projection.
    expected_intervals: tuple[tuple[float, float], ...] | None = None
    # Optional required continuous closed-walk occurrences, not an exact count:
    # additional retraced/subloop walks do not affect simple-loop accuracy.
    expected_closed_walk_intervals: tuple[tuple[float, float], ...] | None = None


def line(vertices, step=8.0):
    vertices = np.asarray(vertices, dtype=float)
    pieces = [np.linspace(a, b, max(1, int(np.ceil(np.linalg.norm(b-a)/step)))+1)[:-1]
              for a, b in zip(vertices[:-1], vertices[1:])]
    return np.vstack([*pieces, vertices[-1:]])


def circle(radius=160.0, laps=1):
    per_lap = max(96, int(np.ceil(2*np.pi*radius/8)))
    angle = np.linspace(0, 2*np.pi*laps, per_lap*laps+1)
    return np.column_stack([radius*np.cos(angle), radius*np.sin(angle)])


def gpx_bytes(points):
    """Return explicit GPX bytes and the exact serialized lon/lat values."""
    points = np.asarray(points, dtype=float)
    lon, lat = TO_WGS84.transform(points[:, 0]+500000, points[:, 1]+4000000)
    coords = np.asarray([[float(f'{x:.10f}'), float(f'{y:.10f}')]
                         for x, y in zip(lon, lat)])
    body = ''.join(f'<trkpt lat="{y:.10f}" lon="{x:.10f}"><ele>100</ele></trkpt>'
                   for x, y in coords)
    raw = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<gpx version="1.1" creator="loop-recognition-synthetic" '
           'xmlns="http://www.topografix.com/GPX/1/1">'
           f'<trk><name>Synthetic</name><trkseg>{body}</trkseg></trk></gpx>')
    return raw.encode('utf-8'), coords


def synthetic_cases():
    yield Case('single_loop', 'radius=160', circle(), 1, 1, True)
    for count in range(2, 7):
        yield Case('repeated_circle', f'laps={count}', circle(laps=count),
                   1, count, True)
    yield Case('open_route', 'three_sides',
               line([[0, 0], [400, 0], [400, 400], [0, 400]]), 0, 0, False)
    yield Case('straight_outback', 'leg=600',
               line([[0, 0], [600, 0], [0, 0]]), 0, 0, True)
    outward = line([[0, 0], [200, 0], [300, 180], [600, 250]])
    yield Case('curved_outback', 'piecewise_curve',
               np.vstack([outward, outward[-2::-1]]), 0, 0, True)
    loop = line([[300, 0], [600, 0], [600, 300], [300, 300], [300, 0]])
    stem = line([[0, 0], [300, 0]])
    for closed in (True, False):
        exit_leg = stem[::-1] if closed else line([[300, 0], [300, -300]])
        points = np.vstack([stem[:-1], loop, exit_leg[1:]])
        yield Case('closed_lollipop' if closed else 'open_lollipop',
                   'square=300,stem=300', points, 1, 1, closed, ((300, 1500),))
    angle = np.linspace(0, 2*np.pi, 321)
    yield Case('figure_eight', 'geometric_crossing_not_road_connectivity',
               np.column_stack([300*np.sin(angle), 200*np.sin(2*angle)]),
               2, 2, True)
    branch = line([[0, 0], [400, 0], [650, 0], [400, 0],
                   [400, 400], [0, 400], [0, 0]])
    yield Case('internal_branch', 'square=400,branch=250', branch,
               0, 0, True, expected_closed_walk_intervals=((0, 2100),))
    yield Case('reverse_internal_branch', 'reverse_ABCDCEA', branch[::-1],
               0, 0, True, expected_closed_walk_intervals=((0, 2100),))
    yield Case('nested_branches', 'nested_outback_in_square',
               line([[0, 0], [400, 0], [650, 0], [650, -200],
                     [650, 0], [400, 0], [400, 400], [0, 400], [0, 0]]),
               0, 0, True, expected_closed_walk_intervals=((0, 2500),))
    yield Case('repeated_branches', 'two_outbacks_in_square',
               line([[0, 0], [400, 0], [650, 0], [400, 0], [650, 0],
                     [400, 0], [400, 400], [0, 400], [0, 0]]),
               0, 0, True, expected_closed_walk_intervals=((0, 2600),))
    yield Case('internal_subloop', 'square=400,subloop=200',
               line([[0, 0], [400, 0], [600, 0], [600, -200],
                     [400, -200], [400, 0], [400, 400], [0, 400], [0, 0]]),
               1, 1, True, ((400, 1200),), ((0, 2400),))
    clean_loop = line([[0, 0], [400, 0], [400, 400], [0, 400], [0, 0]])
    yield Case('branch_then_clean_loop', 'branch_walk_then_square',
               np.vstack([branch, clean_loop[1:]]),
               1, 1, True, ((2100, 3700),), ((0, 2100),))
    forward = circle()
    yield Case('reverse_repeat', 'forward_then_reverse',
               np.vstack([forward, forward[-2::-1]]), 1, 2, True)


def source_intervals(occurrence):
    return [(float(part['start']['s_m']), float(part['end']['s_m']))
            for part in occurrence['source_intervals']]


def boundary_error(occurrence, expected):
    actual = source_intervals(occurrence)
    if len(actual) != len(expected):
        return None
    return max(abs(a-b) for actual_pair, expected_pair in zip(actual, expected)
               for a, b in zip(actual_pair, expected_pair))


def evaluate(case, recognize):
    raw, _ = gpx_bytes(case.points)
    started = perf_counter()
    row = {'category': case.category, 'variant': case.variant,
           'expected_loops': case.expected_loops,
           'expected_occurrences': case.expected_occurrences,
           'expected_closed': case.expected_closed}
    try:
        result = recognize(raw, source_name='synthetic.gpx')
        report = result['report']
        loops = report['loops']
        candidates = report['loop_candidates']
        all_loops = loops + candidates
        occurrences = [occ for loop in all_loops for occ in loop['occurrences']]
        complete_occurrences = [occ for occ in occurrences if occ['status'] == 'complete']
        walks = report['closed_walks']
        walk_occurrences = [occ for walk in walks for occ in walk['occurrences']]
        known = report['has_loops'] is not None
        row.update(status='known' if known else 'unknown',
                   has_loops=report['has_loops'],
                   completeness=report['completeness'],
                   partial=report['completeness'] != 'complete',
                   connectivity_diagnostic=any('connectivity' in str(item).lower()
                       for item in report['limitations'] + [warning
                           for loop in all_loops for warning in loop['warnings']]),
                   confirmed_loops=len(loops), candidate_loops=len(candidates),
                   geometric_loops=len(all_loops), occurrences=len(occurrences),
                   complete_occurrences=len(complete_occurrences),
                   partial_occurrences=sum(occ['status'] == 'partial' for occ in occurrences),
                   closed_walks=len(walks), closed_walk_occurrences=len(walk_occurrences),
                   count_match=(len(all_loops) == case.expected_loops and
                                len(complete_occurrences) == case.expected_occurrences),
                   closure_match=(report['whole_track']['is_closed'] ==
                                  case.expected_closed),
                   limitations=report['limitations'])
        if case.expected_intervals is not None:
            errors = [boundary_error(occ, case.expected_intervals)
                      for occ in complete_occurrences]
            finite = [error for error in errors if error is not None]
            row['source_boundaries'] = {
                'expected_intervals_m': case.expected_intervals,
                'comparable_occurrences': len(finite),
                'best_max_error_m': min(finite) if finite else None,
                'within_25m': bool(finite and min(finite) <= 25),
            }
        if case.expected_closed_walk_intervals is not None:
            required_errors = []
            for expected_interval in case.expected_closed_walk_intervals:
                errors = [boundary_error(occ, (expected_interval,))
                          for occ in walk_occurrences]
                finite = [error for error in errors if error is not None]
                required_errors.append(min(finite) if finite else None)
            row['closed_walk_boundaries'] = {
                'required_intervals_m': case.expected_closed_walk_intervals,
                'best_max_errors_m': required_errors,
                'all_within_25m': all(error is not None and error <= 25
                                     for error in required_errors),
            }
    except Exception as exc:
        # Retain failures in the denominator and preserve their diagnostic.
        row.update(status='error', error=f'{type(exc).__name__}: {exc}')
    row['elapsed_ms'] = round((perf_counter()-started)*1000, 3)
    return row


def summarize(rows):
    known = [row for row in rows if row['status'] == 'known']
    positives = [row for row in known if row['expected_loops'] > 0]
    negatives = [row for row in known if row['expected_loops'] == 0]
    boundaries = [row['source_boundaries'] for row in rows if 'source_boundaries' in row]
    walk_boundaries = [row['closed_walk_boundaries'] for row in rows
                       if 'closed_walk_boundaries' in row]
    return {
        'cases': len(rows), 'known': len(known),
        'unknown': sum(row['status'] == 'unknown' for row in rows),
        'partial': sum(row.get('partial', False) for row in rows),
        'errors': sum(row['status'] == 'error' for row in rows),
        'known_exact_count_matches': sum(row['count_match'] for row in known),
        'known_closure_matches': sum(row['closure_match'] for row in known),
        'known_positive_cases': len(positives),
        'true_positive': sum(row['has_loops'] is True for row in positives),
        'false_negative': sum(row['has_loops'] is False for row in positives),
        'known_negative_cases': len(negatives),
        'false_positive': sum(row['has_loops'] is True for row in negatives),
        'true_negative': sum(row['has_loops'] is False for row in negatives),
        'boundary_cases': len(boundaries),
        'boundary_cases_within_25m': sum(row['within_25m'] for row in boundaries),
        'closed_walks': sum(row.get('closed_walks', 0) for row in rows),
        'closed_walk_occurrences': sum(row.get('closed_walk_occurrences', 0)
                                       for row in rows),
        'closed_walk_boundary_cases': len(walk_boundaries),
        'closed_walk_boundary_cases_within_25m': sum(row['all_within_25m']
                                                    for row in walk_boundaries),
        'elapsed_ms': round(sum(row['elapsed_ms'] for row in rows), 3),
    }


def main():
    from loop_recognition import recognize_bytes
    rows = [evaluate(case, recognize_bytes) for case in synthetic_cases()]
    categories = defaultdict(list)
    for row in rows:
        categories[row['category']].append(row)
    payload = {
        'benchmark': 'synthetic_loop_recognition_v2',
        'scope': 'Exact noiseless synthetic geometry only; not real-road accuracy.',
        'crossing_policy': 'Confirmed plus candidate counts are geometric only; '
                           'a GPX crossing does not prove road connectivity.',
        'unknown_policy': 'Unknown and errors are reported separately, never negatives.',
        'closed_walk_policy': 'Closed walks are measured separately from simple loops. '
                              'Optional interval expectations require inclusion, not exact counts; '
                              'no cumulative multi-lap walk is required.',
        'summary': summarize(rows),
        'categories': {name: summarize(group) for name, group in categories.items()},
        'cases': rows,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if any(row['status'] == 'error' for row in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
