#!/usr/bin/env python3
"""Deterministic synthetic topology benchmark; NOT real-world accuracy.

Run using the repository's cached/offline uv command (see README). Only the JSON
summary persists; end-to-end GPX/GeoJSON/report artifacts use a temporary folder.
No algorithm/configuration is tuned from outcomes. Exit status indicates execution,
not benchmark success: unsupported/failed cases are deliberately retained.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
import route_dedupe as rd

NOISE_METRES = (0.0, 1.0, 3.0, 8.0)
SEEDS = (1729, 104729)
CFG = rd.Config()
COLLAPSE_LAPS = 'repeated_laps_collapse'
COLLAPSE_TAIL = 'reversed_tail_collapse'

# Status means implemented transformation, not whether keeping an input is safe.
SPECS = {
    'open_route': ('Open three-sided rectangle; side 200/500/1000 m.', 'unchanged', None, False),
    'single_loop': ('One circle; radius 100/200/500 m.', 'unchanged', None, False),
    'same_direction_2_to_6_laps': ('2/3/4/5/6 equal full circles; radius 100/200/500 m.', COLLAPSE_LAPS, 'laps', True),
    'standard_400m_laps': ('2/3/4/5/6 full circles of exactly 400 m circumference (not an oval stadium model).', COLLAPSE_LAPS, 'laps', False),
    'closed_loop_tail': ('Square loop, side 300/600/1200 m, with a shared out/back stem of the same length.', COLLAPSE_TAIL, 'tail', True),
    'late_unequal_access': ('Different 1.0/1.2 km access legs, 3.5 km shared stem, 16 km rectangular loop; scales 0.5/1/1.5.', COLLAPSE_TAIL, 'tail', True),
    'pure_out_and_back': ('A straight outward leg of 500/1500/3000 m followed by its exact reverse; safe policy is unchanged, not conversion.', 'unchanged', None, False),
    'internal_turnaround_branch': ('One square loop, side 400/800/1600 m, with an internal visit to a 250/500/1000 m dead-end; safe policy is unchanged, not branch removal.', 'unchanged', None, False),
    'figure_eight': ('One Gerono-like crossing loop x=a*sin(t), y=0.625*a*sin(2*t); a=200/500/800 m; unchanged is preservation, not recognition.', 'unchanged', None, False),
    'parallel_distinct_roads': ('4 km parallel access roads 10/25/50 m apart joined around a 16 km rectangle; roads are ground-truth distinct even inside the match radius.', 'unchanged', None, False),
}


@dataclass
class Case:
    category: str
    variant: str
    points: np.ndarray
    ideal: np.ndarray
    noise_m: float
    seed: int
    laps: int | None = None
    target_slice: tuple[int, int] | None = None


def line(vertices, step=8.0):
    """Include every corner, with points no more than step metres apart."""
    vertices = np.asarray(vertices, dtype=float)
    pieces = [np.linspace(a, b, max(1, int(np.ceil(np.linalg.norm(b-a)/step)))+1)[:-1]
              for a, b in zip(vertices[:-1], vertices[1:])]
    return np.vstack([*pieces, vertices[-1:]])


def circle(radius, laps=1):
    n = max(80, int(np.ceil(2*np.pi*radius/8)))
    t = np.linspace(0, 2*np.pi*laps, n*laps+1)
    return np.column_stack([radius*np.cos(t), radius*np.sin(t)])


def templates():
    for size in (200, 500, 1000):
        p = line([[0, 0], [size, 0], [size, size], [0, size]])
        yield 'open_route', f'side={size}', p, p, None, None
    for radius in (100, 200, 500):
        p = circle(radius)
        yield 'single_loop', f'radius={radius}', p, p, None, None
        for count in range(2, 7):
            yield 'same_direction_2_to_6_laps', f'radius={radius},laps={count}', circle(radius, count), p, count, None
    for count in range(2, 7):
        yield 'standard_400m_laps', f'perimeter=400,laps={count}', circle(400/(2*np.pi), count), circle(400/(2*np.pi)), count, None
    for side in (300, 600, 1200):
        stem = line([[0, 0], [side, 0]])
        loop = line([[side, 0], [side, side], [2*side, side], [2*side, 0], [side, 0]])
        p = np.vstack([stem[:-1], loop, stem[-2::-1]])
        start = len(stem)-1
        yield 'closed_loop_tail', f'side=stem={side}', p, loop, None, (start, start+len(loop))
    for scale in (0.5, 1.0, 1.5):
        access = line(np.array([[0, 0], [0, 500], [500, 500], [4000, 500]])*scale)
        loop = line(np.array([[4000, 500], [4000, 2500], [8000, 2500], [8000, -1500], [4000, -1500], [4000, 500]])*scale)
        returning = line(np.array([[4000, 500], [500, 500], [500, -100], [0, -100], [0, 0]])*scale)
        p = np.vstack([access[:-1], loop, returning[1:]])
        start = len(access)-1
        yield 'late_unequal_access', f'scale={scale}', p, loop, None, (start, start+len(loop))
    for length in (500, 1500, 3000):
        p = line([[0, 0], [length, 0], [0, 0]])
        yield 'pure_out_and_back', f'leg={length}', p, p, None, None
    for side, branch in ((400, 250), (800, 500), (1600, 1000)):
        p = line([[0, 0], [side, 0], [side+branch, 0], [side, 0], [side, side], [0, side], [0, 0]])
        yield 'internal_turnaround_branch', f'side={side},branch={branch}', p, p, None, None
    for amplitude in (200, 500, 800):
        t = np.linspace(0, 2*np.pi, int(amplitude*1.5)+1)
        p = np.column_stack([amplitude*np.sin(t), 0.625*amplitude*np.sin(2*t)])
        yield 'figure_eight', f'amplitude={amplitude}', p, p, None, None
    for gap in (10, 25, 50):
        p = line([[0, 0], [4000, 0], [4000, 2000], [8000, 2000], [8000, -2000], [4000, -2000], [4000, gap], [0, gap], [0, 0]])
        yield 'parallel_distinct_roads', f'gap={gap}', p, p, None, None


def cases():
    for category, variant, points, ideal, laps, target_slice in templates():
        for noise in NOISE_METRES:
            for seed in SEEDS:
                # The same two deterministic noise streams are used for all shapes.
                # All observations, including closure endpoints, jitter independently.
                rng = np.random.default_rng(seed)
                noisy = points + rng.normal(0, noise, points.shape)
                yield Case(category, variant, noisy, ideal, noise, seed, laps, target_slice)


def ratio(values):
    values = list(values)
    n = len(values)
    return {'numerator': int(sum(values)), 'denominator': n,
            'fraction': float(sum(values)/n) if n else None}


def evaluate(case, directory, transform):
    _, expected, detector, implemented = SPECS[case.category]
    coords = np.column_stack(transform.transform(case.points[:, 0]+500000, case.points[:, 1]+4000000))
    source = directory/'input.gpx'
    rd.write_gpx(source, coords, np.full(len(coords), 100.), 'Deterministic synthetic benchmark')
    report, original, cleaned = rd.run(source, directory/'processed', CFG)
    detected = ('tail' if report['retraced_tail'] is not None else
                'laps' if report['identical_laps'] is not None else None)
    detected_count = (report['identical_laps'] or {}).get('count')
    detector_match = detected == detector and (detector != 'laps' or detected_count == case.laps)
    unchanged = np.array_equal(original, cleaned)
    # A positive action needs the right method AND extent/coverage, not just a label.
    # Reference length follows noisy observed travel: this benchmark does not ask
    # the algorithm to denoise path length, a feature it does not implement.
    ideal = case.ideal + [500000, 4000000]
    _, sample_clean = rd.resample(cleaned, 8)
    _, sample_ideal = rd.resample(ideal, 8)
    geometry_error = max(float(cKDTree(sample_ideal).query(sample_clean)[0].max()),
                         float(cKDTree(sample_clean).query(sample_ideal)[0].max()))
    target_length = None
    if detector == 'laps':
        target_length = float(rd.arclength(original)[-1]/case.laps)
    elif detector == 'tail':
        start, end = case.target_slice
        target_length = float(rd.arclength(original[start:end])[-1])
    length_ok = (target_length is None or
                 abs(float(rd.arclength(cleaned)[-1])-target_length) <= max(60., .05*target_length))
    if expected == 'unchanged':
        action_ok = report['method'] == expected and unchanged
    else:
        action_ok = (report['method'] == expected and detector_match and
                     report['output_closed'] and geometry_error <= 30 and length_ok)
    return {
        'category': case.category, 'variant': case.variant, 'noise_sigma_per_axis_m': case.noise_m,
        'seed': case.seed, 'expected_method': expected, 'actual_method': report['method'],
        'implemented_transform': implemented, 'expected_laps': case.laps,
        'detected_kind': detected, 'detected_laps': detected_count,
        'detector_contract_pass': bool(detector_match),
        'positive_detection_pass': bool(detector_match) if detector is not None else None,
        'correct_action_pass': bool(action_ok), 'unchanged_geometry': bool(unchanged),
        'geometry_symmetric_sample_distance_m': geometry_error,
        'reference_observed_length_m': target_length, 'reference_length_pass': bool(length_ok),
        'input_length_m': report['input_length_m'], 'output_length_m': report['output_length_m'],
        'tail_match_start_m': (report['retraced_tail'] or {}).get('match_start_m'),
        'unmatched_access_m': (report['retraced_tail'] or {}).get('unmatched_access_m'),
    }


def summarize(rows):
    return {
        'samples': len(rows),
        'methods': dict(sorted(Counter(r.get('actual_method', 'error') for r in rows).items())),
        'detector_contract': ratio(r['detector_contract_pass'] for r in rows),
        'positive_detection': ratio(r['positive_detection_pass'] for r in rows if r['positive_detection_pass'] is not None),
        'correct_action': ratio(r['correct_action_pass'] for r in rows),
        'execution_errors': sum('error' in r for r in rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'outputs'/'topology-benchmark.json')
    args = parser.parse_args()
    transform = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='gpx-topology-benchmark-') as temp:
        directory = Path(temp)
        for case in cases():
            try:
                rows.append(evaluate(case, directory, transform))
            except Exception as exc:
                detector = SPECS[case.category][2]
                rows.append({'category': case.category, 'variant': case.variant,
                             'noise_sigma_per_axis_m': case.noise_m, 'seed': case.seed,
                             'expected_method': SPECS[case.category][1], 'error': repr(exc),
                             'detector_contract_pass': False, 'correct_action_pass': False,
                             'positive_detection_pass': False if detector else None})
    categories = {}
    for category, (definition, expected, detector, implemented) in SPECS.items():
        selected = [r for r in rows if r['category'] == category]
        categories[category] = {
            'sample_definition': definition, 'expected_method': expected,
            'positive_detector_target': detector, 'implemented_transform': implemented,
            'support_note': ('Existing transform; empirical success is restricted to these fixtures.' if implemented else
                             'No dedicated transformation/classifier support claim; unchanged may only be a safety-policy success.'),
            **summarize(selected),
            'by_noise_sigma_m': {str(n): summarize([r for r in selected if r['noise_sigma_per_axis_m'] == n]) for n in NOISE_METRES},
            'by_variant': {v: summarize([r for r in selected if r['variant'] == v]) for v in sorted({r['variant'] for r in selected})},
        }
    obj = {
        'benchmark': 'deterministic synthetic topology contracts v1',
        'not_real_world_accuracy': True,
        'config': asdict(CFG), 'seeds': SEEDS, 'noise_sigma_per_axis_m': NOISE_METRES,
        'noise_model': 'Independent zero-mean Gaussian jitter per x/y observation, including endpoints. Not a realistic temporally correlated GPS model. Raw spacing <=8m except figure-eight parameter sampling.',
        'scoring': {
            'detector_contract': 'Exact tail/lap evidence and exact lap count for positive targets; absence of these detectors for negatives. Negative passes do NOT establish named topology recognition.',
            'positive_detection': 'Positive targets only, including unsupported 400m laps. Zero denominator means not applicable, NOT 100%.',
            'correct_action': 'For unchanged: exact original/cleaned array equality and method. For collapse: expected method and exact detector/count; closed output; sampled symmetric geometry distance <=30m to ideal one-lap/middle-loop; length difference <=max(60m,5%) from noisy reference.',
            'reference_length': 'Repeated laps: original observed length / true count. Tails: observed noisy middle-loop length. Thus no requirement to denoise GPS-inflated length.',
            'support': 'No named topology classifier exists for negative controls. Pure out/back conversion, internal branch removal, general unique-edge reconstruction and 400m full-lap collapse are not implemented support.',
        },
        'limitations': [
            'Synthetic hand-specified shapes, two noise seeds and fixed default configuration; no labeled real GPX corpus, sampling-rate sweep, altitude, geographic/projection sweep, or confidence interval.',
            'Same seeds across cases are dependent controls, not independent field trials; 0m noise duplicates the two seed cases.',
            '400m uses a circle, not stadium geometry; default min_overlap_m=150 imposes a 450m minimum detected lap period.',
            'Supported repeated-lap periods are all >=628m; no partial laps, mixed direction, missed points, drift, or unequal full-lap durations are modeled.',
            'Preserving a figure-eight or branch is safe in this policy, not successful deduplication/recognition of that topology.',
            'Parallel roads are declared distinct ground truth; collapsing them is an error even if within match radius.',
            'Finite sampled geometry/length tolerances are not proof of graph correctness or navigability.',
            'Do not pool all classes into a claimed overall real-world accuracy: class weights and negative preservation inflate such a number.',
        ],
        'environment': {'python': platform.python_version(),
                        'packages': {n: importlib.metadata.version(n) for n in ('numpy', 'scipy', 'pyproj')},
                        'route_dedupe_sha256': hashlib.sha256((ROOT/'route_dedupe.py').read_bytes()).hexdigest(),
                        'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'total_samples': len(rows), 'categories': categories, 'samples': rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for name, result in categories.items():
        def fmt(key):
            value = result[key]
            return f"{value['numerator']}/{value['denominator']}" if value['denominator'] else 'N/A'
        print(f"{name}: n={result['samples']} detector-contract={fmt('detector_contract')} positive-detection={fmt('positive_detection')} correct-action={fmt('correct_action')} methods={result['methods']}")
    print(f"TOTAL={len(rows)}; execution_errors={sum('error' in r for r in rows)}; output={args.output}")


if __name__ == '__main__':
    main()
