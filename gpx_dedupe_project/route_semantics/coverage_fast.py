"""Bounded NumPy implementation of the same midpoint coverage quadrature."""

import numpy as np

from .coverage import COS_STANDARD, RADIUS


def measure(parts, sizes=(5000,), step=20, max_gap=1000):
    cells = {}
    total = gaps = 0.0
    for part in parts:
        points = np.asarray(part, dtype=float)
        if not len(points):
            continue
        points = points[:, :2]
        if (
            not np.isfinite(points).all()
            or (np.abs(points[:, 0]) > 180).any()
            or (np.abs(points[:, 1]) > 90).any()
        ):
            raise ValueError("invalid WGS84 coordinate")
        for offset in range(0, len(points) - 1, 4096):
            a = points[offset : offset + 4096]
            b = points[offset + 1 : offset + 4097]
            a = a[: len(b)]
            delta = np.radians(b - a)
            lengths = (
                np.hypot(
                    delta[:, 0] * np.cos(np.radians((a[:, 1] + b[:, 1]) / 2)),
                    delta[:, 1],
                )
                * RADIUS
            )
            valid = lengths <= max_gap
            gaps += float(lengths[~valid].sum())
            a, b, lengths = a[valid], b[valid], lengths[valid]
            total += float(lengths.sum())
            counts = np.maximum(1, np.ceil(lengths / step).astype(np.int64))
            n = int(counts.sum())
            if not n:
                continue
            indices = np.repeat(np.arange(len(a)), counts)
            starts = np.repeat(np.cumsum(counts) - counts, counts)
            t = (np.arange(n) - starts + 0.5) / counts[indices]
            p = a[indices] + t[:, None] * (b - a)[indices]
            xy = np.column_stack(
                (
                    RADIUS * np.radians(p[:, 0]) * COS_STANDARD,
                    RADIUS * np.sin(np.radians(p[:, 1])) / COS_STANDARD,
                )
            )
            weights = (lengths / counts)[indices]
            for size in sizes:
                grid = np.floor(xy / size).astype(np.int64)
                keys, inverse = np.unique(grid, axis=0, return_inverse=True)
                values = np.bincount(inverse, weights=weights)
                for (x, y), value in zip(keys, values):
                    key = (size, int(x), int(y))
                    cells[key] = cells.get(key, 0) + float(value)
    return cells, total, gaps
