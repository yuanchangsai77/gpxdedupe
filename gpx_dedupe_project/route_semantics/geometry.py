"""Shared metric geometry for critical-point extraction and validation."""

import math
from itertools import pairwise


def distance(a, b):
    lat = math.radians((a[1] + b[1]) / 2)
    return (
        math.hypot(math.radians(a[0] - b[0]) * math.cos(lat), math.radians(a[1] - b[1]))
        * 6371000
    )


def length(points):
    return sum(distance(a, b) for a, b in pairwise(points))


def point_segment(p, a, b):
    scale = math.cos(math.radians(p[1]))
    x = (b[0] - a[0]) * scale
    y = b[1] - a[1]
    t = (
        max(
            0,
            min(1, (((p[0] - a[0]) * scale * x + (p[1] - a[1]) * y) / (x * x + y * y))),
        )
        if x * x + y * y
        else 0
    )
    return distance(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
