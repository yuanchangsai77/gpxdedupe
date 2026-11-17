#!/usr/bin/env python3
"""Evaluate explicit user labels; prints JSON only, never copies private tracks.

The labeled routes were already explored. Neither this development collection nor
its prospective calibration-excluded subset measures independent real accuracy.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loop_recognition import classify_route


def evaluate(folder, label_file=None, calibration_only=False):
    label_file = Path(label_file or Path(__file__).with_name('route_shape_labels.json'))
    spec = json.loads(label_file.read_text(encoding='utf-8'))
    excluded = set(spec['calibration_excluded'])
    rows = []
    for name, expected in spec['labels'].items():
        if calibration_only and name in excluded:
            continue
        path = Path(folder) / name
        row = {'file': name, 'expected': expected,
               'split': 'calibration_excluded' if name in excluded else 'calibration'}
        if not path.is_file():
            row.update(status='missing', actual=None, match=False)
        else:
            started = time.perf_counter()
            try:
                result = classify_route(path)
                row.update(status=result['status'], actual=result['category'],
                           match=result['category'] == expected, reasons=result['reasons'],
                           reason_code=result.get('reason_code'), metrics=result['metrics'],
                           limitations=result['limitations'])
            except Exception as exc:
                row.update(status='error', actual=None, match=False,
                           error=type(exc).__name__)
            row['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
    matrix = {str(expected): {str(actual): 0 for actual in (1, 2, 3, 4)}
              for expected in (1, 2, 3, 4)}
    for row in rows:
        if row['actual'] is not None:
            matrix[str(row['expected'])][str(row['actual'])] += 1
    return {'benchmark': 'user_whole_route_shape_development_v1',
            'scope': spec['source'], 'split_caveat': spec['split_caveat'],
            'summary': {'labeled_cases': len(rows),
                        'matches': sum(row['match'] for row in rows),
                        'classified': sum(row['status'] == 'classified' for row in rows),
                        'failed': sum(row['actual'] == 1 for row in rows),
                        'missing': sum(row['status'] == 'missing' for row in rows),
                        'errors': sum(row['status'] == 'error' for row in rows)},
            'expected_counts': dict(Counter(str(row['expected']) for row in rows)),
            'confusion_matrix': matrix, 'rows': rows,
            'unlabeled_not_scored': spec['unlabeled']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', required=True, type=Path)
    parser.add_argument('--calibration-only', action='store_true')
    args = parser.parse_args()
    if not args.folder.is_dir():
        parser.error('GPX folder does not exist')
    report = evaluate(args.folder, calibration_only=args.calibration_only)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    # A mismatch is a retained result, not an execution failure.
    return 1 if report['summary']['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
