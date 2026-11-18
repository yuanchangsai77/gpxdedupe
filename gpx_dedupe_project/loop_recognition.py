"""Standalone, read-only GPX loop recognition (Python 3.10+).

Requires numpy, scipy and pyproj; no other project files or web server are needed.
Run: python loop_recognition.py input.gpx [--out new-report.json]
This is not a road-network reconstruction or an enumeration of all simple cycles.
Source intervals always refer to the unmodified input, including fractional edges.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial import ConvexHull, QhullError, cKDTree
from scipy.spatial.distance import pdist
from pyproj import Transformer


GPX_NAMESPACE = 'http://www.topografix.com/GPX/1/1'


def _tag(name):
    return f'{{{GPX_NAMESPACE}}}{name}'


def _projection(lonlat):
    # Keep the same local UTM selection and arithmetic as the original engine.
    # Input span/polar admission remains in _load below.
    meanlon, meanlat = np.mean(lonlat, axis=0)
    zone = min(60, max(1, int((meanlon + 180) // 6) + 1))
    epsg = (32600 if meanlat >= 0 else 32700) + zone
    forward = Transformer.from_crs('EPSG:4326', f'EPSG:{epsg}', always_xy=True)
    xy = np.column_stack(forward.transform(lonlat[:, 0], lonlat[:, 1]))
    return xy, epsg


def _arclength(xy):
    return np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]


def _tangent(points):
    delta = np.empty_like(points)
    window = min(3, max(1, len(points) // 8))
    for index in range(len(points)):
        delta[index] = points[min(len(points) - 1, index + window)] - points[max(0, index - window)]
    norm = np.linalg.norm(delta, axis=1)
    return delta / np.maximum(norm[:, None], 1.e-9)


class RecognitionError(ValueError):
    """Invalid input or unsupported resource/projection requirements."""


@dataclass(frozen=True)
class LoopRecognitionConfig:
    spacing_m: float = 8.0
    match_radius_m: float = 12.0
    closure_radius_m: float = 25.0
    min_loop_length_m: float = 80.0
    max_input_bytes: int = 16 * 1024 * 1024
    max_points: int = 50_000
    max_samples: int = 12_000
    max_spatial_pairs: int = 250_000
    max_crossing_pairs: int = 250_000
    max_graph_edges: int = 20_000
    max_loops: int = 128
    max_occurrences: int = 2048


def _config(config):
    if config is None:
        config = LoopRecognitionConfig()
    if type(config) is dict:
        try:
            config = LoopRecognitionConfig(**config)
        except TypeError as exc:
            raise RecognitionError('识别配置字段无效。') from exc
    if not isinstance(config, LoopRecognitionConfig):
        raise RecognitionError('识别配置必须是 LoopRecognitionConfig 或配置对象。')
    for field in fields(config):
        value = getattr(config, field.name)
        if field.name.startswith('max_'):
            if type(value) is not int or not 1 <= value <= getattr(LoopRecognitionConfig(), field.name):
                raise RecognitionError(f'{field.name} 必须是默认上限以内的正整数。')
        elif type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise RecognitionError(f'{field.name} 必须是正的有限数值。')
    if not 1 <= config.spacing_m <= 20 or not 1 <= config.match_radius_m <= 30:
        raise RecognitionError('采样间距支持 1–20 米，匹配半径支持 1–30 米。')
    if not 1 <= config.closure_radius_m <= 100 or not 20 <= config.min_loop_length_m <= 100_000:
        raise RecognitionError('闭合半径或最小环长度超出支持范围。')
    return config


def _load(raw, cfg):
    if not isinstance(raw, bytes) or len(raw) > cfg.max_input_bytes:
        raise RecognitionError('GPX 输入超过字节限制或不是字节快照。')
    upper = raw.upper()
    if b'\x00' in raw or b'<!DOCTYPE' in upper or b'<!ENTITY' in upper:
        raise RecognitionError('不支持 DTD、实体声明或含 NUL 的 XML。')
    try:
        root = ET.fromstring(raw)
        segs = root.findall('.//' + _tag('trkseg'))
        if len(segs) != 1:
            raise RecognitionError('环识别仅支持恰好一个 trkseg，不会跨断点连接。')
        points = segs[0].findall(_tag('trkpt'))
        if not 3 <= len(points) <= cfg.max_points:
            raise RecognitionError('轨迹点数超出支持范围。')
        coords = np.asarray([[float(p.attrib['lon']), float(p.attrib['lat'])] for p in points])
        if (not np.isfinite(coords).all() or np.any(np.abs(coords[:, 0]) > 180)
                or np.any(np.abs(coords[:, 1]) > 90)):
            raise RecognitionError('经纬度必须是有效的有限数值。')
        for point in points:
            elevation = point.findtext(_tag('ele'))
            if elevation is not None and not math.isfinite(float(elevation)):
                raise RecognitionError('海拔必须是有限数值。')
    except RecognitionError:
        raise
    except (ET.ParseError, ValueError, KeyError, TypeError, OverflowError, LookupError) as exc:
        raise RecognitionError('GPX 格式或坐标无效。') from exc
    return _prepare_coordinates(coords, cfg)


def _prepare_coordinates(coords, cfg):
    coords = np.asarray(coords, dtype=float)
    if (coords.ndim != 2 or coords.shape[1] != 2 or not 3 <= len(coords) <= cfg.max_points
            or not np.isfinite(coords).all() or np.any(np.abs(coords[:, 0]) > 180)
            or np.any(np.abs(coords[:, 1]) > 90)):
        raise RecognitionError('经纬度或轨迹点数无效。')
    if (np.ptp(coords[:, 0]) > 6 or np.ptp(coords[:, 1]) > 6
            or np.max(coords[:, 1]) > 84 or np.min(coords[:, 1]) < -80):
        raise RecognitionError('轨迹超出局部 UTM 支持范围；不支持跨日期变更线或大跨度轨迹。')
    xy, epsg = _projection(coords)
    distances = _arclength(xy)
    if not np.isfinite(xy).all() or not math.isfinite(float(distances[-1])) or distances[-1] <= 0:
        raise RecognitionError('轨迹投影或长度无效。')
    return coords, xy, distances, epsg


def _noise_diagnostic(xy, cfg):
    # Curvature is NOT interpreted as an estimate of GPS sigma. Only frequent
    # abrupt reversals at a small scale flag unsupported jitter/stop clouds.
    delta = np.diff(xy, axis=0)
    length = np.linalg.norm(delta, axis=1)
    good = length > .1
    direction = delta / np.maximum(length[:, None], 1e-9)
    turn = np.sum(direction[:-1] * direction[1:], axis=1)
    small = np.maximum(length[:-1], length[1:]) <= 4 * cfg.match_radius_m
    valid = good[:-1] & good[1:]
    abrupt = valid & small & (turn < -.3)
    return len(abrupt) >= 20 and np.mean(abrupt) > .18


def _position(s, distances):
    """Rightmost duplicate vertex wins, except the final position uses last edge."""
    edge = min(len(distances) - 2, max(0, int(np.searchsorted(distances, s, side='right')) - 1))
    length = distances[edge + 1] - distances[edge]
    t = float(np.clip((s - distances[edge]) / length, 0, 1)) if length > 1e-9 else 1.
    return {'segment_id': 0, 'edge_start_index': edge, 'edge_end_index': edge + 1,
            't': t, 's_m': float(s)}


def _exact_retraces(coords, distances, cfg):
    """Bounded original-vertex palindromes; never infer matching road identity.

    Only consecutive duplicate observations are collapsed in this analysis view.
    Different vertex densities, near matches and partial edge retraces are outside
    this detector's scope. No loop-erased geometry leaves this helper.
    """
    keep = np.r_[True, np.any(coords[1:] != coords[:-1], axis=1)]
    indices = np.flatnonzero(keep)
    points = coords[indices]
    ranges = []
    comparisons = 0
    limits = []
    for center in range(1, len(points) - 1):
        radius = 1
        matched = 0
        exhausted = False
        while center - radius >= 0 and center + radius < len(points):
            if comparisons >= cfg.max_spatial_pairs:
                limits.append('retrace_comparison_limit')
                exhausted = True
                break
            comparisons += 1
            if not np.array_equal(points[center - radius], points[center + radius]):
                break
            matched = radius
            radius += 1
        if exhausted:
            # A truncated expansion is not a completed maximal annotation.
            break
        if matched:
            ranges.append((int(indices[center - matched]), int(indices[center]),
                           int(indices[center + matched])))
    # Sort outer intervals first. Nested palindrome fragments add no new travel.
    maximal = []
    furthest = -1
    for start, turn, end in sorted(ranges, key=lambda item: (item[0], -item[2], item[1])):
        if end <= furthest:
            continue
        maximal.append((start, turn, end))
        furthest = end
    if len(maximal) > cfg.max_occurrences:
        maximal = maximal[:cfg.max_occurrences]
        limits.append('retrace_annotation_limit')
    annotations = []
    total = float(distances[-1])
    for start, turn, end in maximal:
        a, b = float(distances[start]), float(distances[end])
        if b <= a:
            continue
        length = b - a
        whole = abs(a) <= 1e-7 and abs(b - total) <= 1e-7
        annotations.append({
            'id': f'retrace-{len(annotations) + 1:03d}',
            'source_interval': {'start': _position(a, distances), 'end': _position(b, distances)},
            'start_vertex_index': start, 'turn_vertex_index': turn, 'end_vertex_index': end,
            'turnaround': _position(float(distances[turn]), distances),
            'length_m': length, 'fraction_of_track': length / total,
            'measurement': 'exact_original_geometry',
            'is_short_local': length <= 100 and length / total <= .05 and not whole,
            'evidence': {'method': 'original_reverse_vertex_sequence',
                         'boundary_basis': 'original_vertices', 'max_reverse_error_m': 0.},
            'warnings': ['exact_recorded_coordinates_do_not_verify_road_identity'],
        })
    return annotations, sorted(set(limits))


def _route_summary(coords, distances, gap, annotations, limits, cfg):
    """Describe full source and measured retraces, not a hypothetical ring body."""
    union = []
    for annotation in annotations:
        interval = annotation['source_interval']
        a, b = interval['start']['s_m'], interval['end']['s_m']
        if union and a <= union[-1][1]:
            union[-1][1] = max(union[-1][1], b)
        else:
            union.append([a, b])
    length = sum(b - a for a, b in union)
    total = float(distances[-1])
    closure = ('exact_endpoint' if np.array_equal(coords[0], coords[-1]) else
               'within_tolerance' if gap <= cfg.closure_radius_m else 'open')
    return {'source_interval': {'start': _position(0., distances), 'end': _position(total, distances)},
            'length_m': total, 'closure_kind': closure,
            'retrace_annotation_ids': [annotation['id'] for annotation in annotations],
            'retrace_union_length_m': length, 'retrace_union_fraction': length / total,
            'retrace_measurement': 'exact_original_geometry',
            'annotation_status': 'partial' if limits else 'complete', 'limitations': limits,
            'method': 'original_coordinate_palindromes_v1',
            'short_local_rule': {'max_length_m': 100., 'max_fraction': .05}}


def _sample(xy, distances, cfg):
    # Uniform arclength plus genuine input corners; no rounding of source indices.
    if math.ceil(distances[-1] / cfg.spacing_m) + 1 > cfg.max_samples:
        raise RecognitionError('分析采样点超过限制，请缩短轨迹或增大间距。')
    lengths = np.diff(distances)
    keep = np.r_[True, lengths > 1e-7]
    unique_s, unique_xy = distances[keep], xy[keep]
    delta = np.diff(unique_xy, axis=0)
    norm = np.linalg.norm(delta, axis=1)
    directions = delta / np.maximum(norm[:, None], 1e-9)
    corners = np.flatnonzero((np.sum(directions[:-1] * directions[1:], axis=1) < .85)
                             & (np.minimum(norm[:-1], norm[1:]) >= cfg.spacing_m / 4)) + 1
    s = np.unique(np.r_[np.arange(0, distances[-1], cfg.spacing_m), unique_s[corners], distances[-1]])
    if len(s) > cfg.max_samples:
        raise RecognitionError('保留转角后的采样点超过限制。')
    q = np.column_stack([np.interp(s, unique_s, unique_xy[:, axis]) for axis in range(2)])
    return s, q


def _crossings(s, q, cfg):
    """Bounded segment-index broad phase; crossings are hypotheses, not road nodes."""
    mid = (q[:-1] + q[1:]) / 2
    half = np.linalg.norm(np.diff(q, axis=0), axis=1) / 2
    tree = cKDTree(mid)
    radius = max(float(half.max()) * 2, 1e-7)
    counts = tree.query_ball_point(mid, radius, return_length=True)
    if (int(counts.sum()) - len(mid)) // 2 > cfg.max_crossing_pairs:
        return s, q, set(), ['crossing_pair_limit']
    extra = []
    cross_s = set()
    min_sep = max(3 * cfg.spacing_m, 4 * cfg.match_radius_m)
    for i, center in enumerate(mid):
        for j in sorted(tree.query_ball_point(center, radius)):
            if j <= i + 1 or s[j] - s[i + 1] < min_sep:
                continue
            a, b = q[i], q[j]
            u, v = q[i + 1] - a, q[j + 1] - b
            determinant = float(u[0] * v[1] - u[1] * v[0])
            # Collinear/near-tangent repeated traversals are corridor matches,
            # not crossing events. Tiny numerical angles otherwise manufacture
            # thousands of artificial nodes on repeated circles.
            if abs(determinant) < max(1e-8, .2 * float(np.linalg.norm(u) * np.linalg.norm(v))):
                continue
            offset = b - a
            t = float((offset[0] * v[1] - offset[1] * v[0]) / determinant)
            w = float((offset[0] * u[1] - offset[1] * u[0]) / determinant)
            if not (0 <= t <= 1 and 0 <= w <= 1):
                continue
            for k, fraction in ((i, t), (j, w)):
                value = float(s[k] + fraction * (s[k + 1] - s[k]))
                extra.append(value)
                cross_s.add(round(value, 6))
    values = np.unique(np.r_[s, extra])
    if len(values) > cfg.max_samples:
        return s, q, set(), ['crossing_sample_limit']
    points = np.column_stack([np.interp(values, s, q[:, axis]) for axis in range(2)])
    return values, points, cross_s, []


def _intervals(edges, s, distances):
    spans = []
    for a, b in edges:
        start, end = float(s[a]), float(s[b])
        if end - start < 1e-7:
            continue
        if spans and abs(spans[-1][1] - start) < 1e-6:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return [{'start': _position(a, distances), 'end': _position(b, distances)} for a, b in spans]


def _shape(points, step, maximum=1024):
    """Closed representation used only for ordered comparison, never output as a GPX."""
    closed = np.vstack([points, points[:1]]) if np.linalg.norm(points[-1] - points[0]) > 1e-8 else points
    d = _arclength(closed)
    unique = np.r_[True, np.diff(d) > 1e-7]
    d, closed = d[unique], closed[unique]
    n = max(32, min(maximum, int(math.ceil(d[-1] / step))))
    phases = np.linspace(0, d[-1], n, endpoint=False)
    return np.column_stack([np.interp(phases, d, closed[:, axis]) for axis in range(2)])


def _compare(reference, points, cfg):
    """Ordered cyclic alignment (not Hausdorff/edge-set identity)."""
    shape = _shape(points, cfg.spacing_m, len(reference))
    if len(shape) != len(reference):
        shape = _shape(points, .0001, len(reference))
    best = None
    for sign in (1, -1):
        oriented = shape if sign == 1 else shape[::-1]
        _, anchors = cKDTree(oriented).query(reference[0], k=min(3, len(oriented)))
        for anchor in np.atleast_1d(anchors):
            aligned = np.roll(oriented, -int(anchor), axis=0)
            residual = aligned - reference
            error = np.linalg.norm(residual, axis=1)
            if np.quantile(error, .9) > cfg.match_radius_m or np.mean(error <= cfg.match_radius_m) < .9:
                continue
            # A sustained separate band is evidence against merging nearby roads.
            heading = _tangent(reference)
            lateral = heading[:, 0] * residual[:, 1] - heading[:, 1] * residual[:, 0]
            median = float(np.median(lateral))
            stable = float(np.mean(lateral * median > 0))
            if abs(median) >= max(2., cfg.match_radius_m * .2) and stable > .85:
                continue
            shift = np.median(residual, axis=0)
            if np.linalg.norm(shift) >= max(3., cfg.match_radius_m * .3) and np.quantile(np.linalg.norm(residual - shift, axis=1), .75) < 2:
                continue
            score = float(np.quantile(error, .9))
            if best is None or score < best[0]:
                best = (score, sign)
    return best


def _continuous_record(start, end, s, q, distances, xy, cfg, cross_s, internal):
    """Publish the actual continuous traversal, never the loop-erased stack."""
    a, b = float(s[start]), float(s[end])
    middle = xy[(distances > a) & (distances < b)]
    first = np.asarray([np.interp(a, distances, xy[:, axis]) for axis in range(2)])
    last = np.asarray([np.interp(b, distances, xy[:, axis]) for axis in range(2)])
    points = np.vstack([first, middle, last])
    centered = points - points.mean(axis=0)
    _, _, axes = np.linalg.svd(centered, full_matrices=False)
    width = float(np.ptp(centered @ axes[-1]))
    # Ordered reverse correspondence detects curved pure retraces as well as
    # straight ones; it is evidence, not an assertion of physical road identity.
    d = _arclength(points)
    probes = np.linspace(0, d[-1], min(301, max(21, len(points))))
    p = np.column_stack([np.interp(probes, d, points[:, axis]) for axis in range(2)])
    reverse_fraction = float(np.mean(np.linalg.norm(p - p[::-1], axis=1) <= cfg.match_radius_m))
    retrace = width <= 1.5 or reverse_fraction >= .85
    # Sampling's return-separation threshold can hide arbitrarily short P-Q-P
    # excursions. Check the actual source vertices before claiming simplicity;
    # this must not depend on the optional annotation scan or its budget.
    unique_points = points[np.r_[True, np.any(points[1:] != points[:-1], axis=1)]]
    local_reverse = (len(unique_points) >= 3 and
                     bool(np.any(np.all(unique_points[:-2] == unique_points[2:], axis=1))))
    repeated = bool(internal) or retrace or local_reverse
    warnings = ['geometry_only_road_connectivity_unverified']
    candidate = b - a < cfg.min_loop_length_m or width < 2 * cfg.match_radius_m
    if b - a < cfg.min_loop_length_m:
        warnings.append('below_resolution')
    if width < 2 * cfg.match_radius_m and not retrace:
        warnings.append('narrow_loop_or_displaced_retrace')
    crossing = round(a, 6) in cross_s or round(b, 6) in cross_s
    observed = min(abs(distances - a)) < 1e-4 and min(abs(distances - b)) < 1e-4
    if crossing:
        warnings.append('crossing_connectivity_unresolved')
        if not observed:
            candidate = True
            warnings.append('interpolated_crossing_only')
    occurrence = {'id': '', 'direction': 'forward', 'status': 'complete',
                  'source_intervals': [{'start': _position(a, distances), 'end': _position(b, distances)}],
                  'excluded_intervals': [], 'start_m': a, 'end_m': b,
                  'length_m': b - a, 'closure_gap_m': float(np.linalg.norm(last - first))}
    return {'kind': 'closed_walk' if repeated else 'simple_loop', 'points': points,
            'candidate': candidate, 'warnings': warnings, 'occurrence': occurrence, 'width': width,
            'contains_retrace': retrace or local_reverse or any(item['retrace'] for item in internal),
            'contains_subloops': any(item['simple'] for item in internal),
            'reverse_match_fraction': reverse_fraction}


def _decompose(s, q, distances, xy, cfg, cross_s, limits):
    radius = min(cfg.match_radius_m, cfg.spacing_m * .75)
    separation = max(3 * cfg.spacing_m, 4 * cfg.match_radius_m)
    tree = cKDTree(q)
    counts = tree.query_ball_point(q, radius, return_length=True)
    if (int(counts.sum()) - len(q)) // 2 > cfg.max_spatial_pairs:
        return [], [], [], [], [*limits, 'spatial_pair_limit'], []
    stack = []
    active = {}  # Original sampled node -> stack slot (no transitive clustering).
    records = []
    events = []
    ignored = []
    history = []
    walks = []
    for i in range(len(s)):
        nearest = []
        for j in tree.query_ball_point(q[i], radius):
            slot = active.get(j)
            if slot is not None and s[i] - s[stack[slot]['exit']] >= separation:
                nearest.append((float(np.linalg.norm(q[i] - q[j])), j, slot))
        if not nearest:
            active[i] = len(stack)
            stack.append({'anchor': i, 'entry': i, 'exit': i})
            if len(stack) > cfg.max_graph_edges:
                limits.append('graph_edge_limit')
                break
            continue
        _, j, slot = min(nearest)
        # A radius entry is not the actual return boundary. Prefer the local
        # distance minimum, including a retained corner or an inserted crossing,
        # so adjacent passes do not accumulate a missing wedge on every lap.
        upcoming = np.arange(i + 1, int(np.searchsorted(s, s[i] + 2 * radius, side='right')))
        if len(upcoming) and np.min(np.linalg.norm(q[upcoming] - q[j], axis=1)) < np.linalg.norm(q[i] - q[j]) - 1e-5:
            active[i] = len(stack)
            stack.append({'anchor': i, 'entry': i, 'exit': i})
            if len(stack) > cfg.max_graph_edges:
                limits.append('graph_edge_limit')
                break
            continue
        start = stack[slot]['exit']
        internal = [item for item in history if item['end'] > start and item['start'] >= start
                    and item['end'] <= i and item['end'] - item['start'] > 1]
        record = _continuous_record(start, i, s, q, distances, xy, cfg, cross_s, internal)
        retrace = record['contains_retrace']
        simple = record['kind'] == 'simple_loop'
        history.append({'start': start, 'end': i, 'retrace': retrace, 'simple': simple})
        # Tiny pure return fragments are evidence for ancestor classification,
        # not hundreds of independent output walks along one curved retrace.
        if record['occurrence']['length_m'] >= min(cfg.min_loop_length_m, 30.):
            if simple:
                records.append(record)
            elif not (retrace and not internal):
                # Retain maximal nested closed walks; never alter their sources.
                walks = [walk for walk in walks if not (
                    record['occurrence']['start_m'] <= walk['occurrence']['start_m']
                    and record['occurrence']['end_m'] >= walk['occurrence']['end_m'])]
                walks.append(record)
            else:
                ignored.append({'start_m': float(s[start]), 'end_m': float(s[i]), 'reason': 'retrace_evidence'})
            events.append({'kind': 'geometric_return', 'first': _position(float(s[start]), distances),
                           'return': _position(float(s[i]), distances),
                           'gap_m': record['occurrence']['closure_gap_m'],
                           'structure_kind': record['kind'], 'crossing_connectivity_verified': False})
            if len(records) + len(walks) >= cfg.max_occurrences:
                limits.append('occurrence_limit')
                break
        # Analysis-only chronological loop erasure. Keep entry AND latest exit:
        # the following retained edge must start after, not before, the excursion.
        for removed in stack[slot + 1:]:
            active.pop(removed['anchor'], None)
        stack = stack[:slot + 1]
        stack[-1]['exit'] = i
        if len(stack) > cfg.max_graph_edges:
            limits.append('graph_edge_limit')
            break
    # Whole-track fallback keeps an observed closed journey with retracing in
    # full. Do not manufacture cumulative two/three-lap combinations.
    if (not limits and history and any(item['retrace'] for item in history)
            and np.linalg.norm(xy[-1] - xy[0]) <= radius):
        full = _continuous_record(0, len(s) - 1, s, q, distances, xy, cfg, cross_s, history)
        # Keep prior chronological walks (e.g. a branch-bearing first lap)
        # when a later clean lap follows. The full-track fallback is additional
        # provenance, never a replacement for an observed earlier traversal.
        same_range = any(abs(walk['occurrence']['start_m']) < 1e-6
                         and abs(walk['occurrence']['end_m'] - s[-1]) < 1e-6 for walk in walks)
        if not same_range:
            if len(records) + len(walks) + 1 <= cfg.max_occurrences:
                walks.append(full)
            else:
                limits.append('occurrence_limit')
    return records, stack, events, ignored, limits, walks


def _group(records, cfg, limits, reserved=0):
    groups = []
    for record in records:
        occurrence = record['occurrence']
        matching = None
        for group in groups:
            if group['candidate'] != record['candidate']:
                continue
            length_ratio = occurrence['length_m'] / group['loop']['length_m']
            if not .85 <= length_ratio <= 1.15:
                continue
            match = _compare(group['shape'], record['points'], cfg)
            if match is not None:
                matching = group
                occurrence['direction'] = 'forward' if match[1] == 1 else 'reverse'
                break
        if matching is None:
            if len(groups) + reserved >= cfg.max_loops:
                limits.append('loop_limit')
                break
            identifier = f"loop-{len(groups) + 1:03d}"
            loop = {'id': identifier, 'length_m': occurrence['length_m'],
                    'closure_gap_m': occurrence['closure_gap_m'],
                    'evidence': {'method': 'ordered_chronological_return', 'spatial_width_m': record['width'],
                                 'road_identity_verified': False},
                    'warnings': record['warnings'], 'occurrences': []}
            matching = {'loop': loop, 'candidate': record['candidate'],
                        'shape': _shape(record['points'], cfg.spacing_m), 'points': record['points']}
            groups.append(matching)
        occurrence['id'] = f"occurrence-{len(matching['loop']['occurrences']) + 1:03d}"
        matching['loop']['occurrences'].append(occurrence)
    return groups


def _partial_occurrence(groups, stack, s, q, distances, cfg, limits, reserved=0):
    """Match bounded ordered subfragments, without inventing a closing edge.

    Access/exit edges are not required to lie on the loop. Source gaps end a
    partial occurrence: removing an excursion cannot create a traversal.
    """
    if len(stack) < 3 or not groups:
        return
    edges = [(a['exit'], b['entry']) for a, b in zip(stack[:-1], stack[1:])]
    starts = np.asarray([q[a] for a, _ in edges])
    ends = np.asarray([q[b] for _, b in edges])
    lengths = np.asarray([s[b] - s[a] for a, b in edges])
    proposals = []
    for group in groups:
        if group['candidate']:
            continue
        shape = group['shape']
        count = len(shape)
        tree = cKDTree(shape)
        first_error, first_phase = tree.query(starts)
        last_error, last_phase = tree.query(ends)
        middle_error, _ = tree.query((starts + ends) / 2)
        delta = (last_phase - first_phase + count / 2) % count - count / 2
        covered = delta / count * group['loop']['length_m']
        valid = ((np.maximum(first_error, last_error) <= cfg.match_radius_m / 2)
                 & (middle_error <= cfg.match_radius_m / 2)
                 & (np.abs(np.abs(covered) - lengths) <= max(2 * cfg.spacing_m, cfg.match_radius_m)))
        # Split at unmatched access/exit edges and direction reversals. Zero
        # nearest-phase steps are allowed at interpolation boundaries.
        runs = []
        run, sign = [], 0
        for index, good in enumerate(valid):
            direction = int(np.sign(delta[index]))
            source_gap = bool(run and edges[index][0] != edges[run[-1]][1])
            if not good or source_gap or (sign and direction and direction != sign):
                if run:
                    runs.append((run, sign))
                run, sign = [], 0
            if good:
                run.append(index)
                sign = direction or sign
        if run:
            runs.append((run, sign))
        for indices, sign in runs:
            length = float(lengths[indices].sum())
            ratio = length / group['loop']['length_m']
            if not sign or not .08 <= ratio < 1 or length < max(4 * cfg.spacing_m, 2 * cfg.match_radius_m):
                continue
            phase_length = float(abs(covered[indices].sum()))
            if abs(phase_length - length) > max(2 * cfg.spacing_m, .15 * length):
                continue
            selected_edges = [edges[index] for index in indices]
            intervals = _intervals(selected_edges, s, distances)
            if intervals:
                proposals.append((group, sign, intervals, length, selected_edges))
    for group, sign, intervals, length, selected_edges in proposals:
        start, end = intervals[0]['start']['s_m'], intervals[-1]['end']['s_m']
        # An overlapping explanation by another known loop is ambiguous.
        if any(other is not group and min(end, parts[-1]['end']['s_m']) > max(start, parts[0]['start']['s_m'])
               for other, _, parts, _, _ in proposals):
            continue
        if reserved + sum(len(item['loop']['occurrences']) for item in groups) >= cfg.max_occurrences:
            limits.append('occurrence_limit')
            return
        if len(intervals) != 1:
            continue
        excluded = []
        occurrence = {'id': f"occurrence-{len(group['loop']['occurrences']) + 1:03d}",
                      'direction': 'forward' if sign == 1 else 'reverse', 'status': 'partial',
                      'source_intervals': intervals, 'excluded_intervals': excluded,
                      'start_m': start, 'end_m': end, 'length_m': length,
                      'closure_gap_m': float(np.linalg.norm(q[selected_edges[-1][1]] - q[selected_edges[0][0]]))}
        group['loop']['occurrences'].append(occurrence)


def recognize_bytes(raw: bytes, config=None, source_name='track.gpx'):
    """Analyze an immutable byte snapshot. Never writes files or invokes transforms."""
    cfg = _config(config)
    coords, xy, distances, epsg = _load(raw, cfg)
    annotations, annotation_limits = _exact_retraces(coords, distances, cfg)
    s, q = _sample(xy, distances, cfg)
    s, q, crossings, limits = _crossings(s, q, cfg)
    records, stack, events, ignored, limits, walk_records = _decompose(s, q, distances, xy, cfg, crossings, limits)
    if _noise_diagnostic(xy, cfg):
        limits.append('noise_above_supported_resolution')
        for record in records:
            record['candidate'] = True
            record['warnings'].append('noise_above_supported_resolution')
    if len(walk_records) > cfg.max_loops:
        walk_records = walk_records[:cfg.max_loops]
        limits.append('loop_limit')
    groups = _group(records, cfg, limits, reserved=len(walk_records))
    if not limits:
        _partial_occurrence(groups, stack, s, q, distances, cfg, limits, reserved=len(walk_records))
    closed_walks = []
    for index, record in enumerate(walk_records):
        occurrence = record['occurrence']
        occurrence['id'] = 'occurrence-001'
        closed_walks.append({'id': f'walk-{index + 1:03d}', 'kind': 'closed_walk',
                             'length_m': occurrence['length_m'], 'closure_gap_m': occurrence['closure_gap_m'],
                             'contains_retrace': record['contains_retrace'],
                             'contains_subloops': record['contains_subloops'],
                             'warnings': record['warnings'], 'occurrences': [occurrence]})
    loops = [g['loop'] for g in groups if not g['candidate']]
    candidates = [g['loop'] for g in groups if g['candidate']]
    gap = float(np.linalg.norm(xy[-1] - xy[0]))
    relations = []
    for index, first in enumerate(groups):
        for second in groups[index + 1:]:
            distances_to_first, _ = cKDTree(first['shape']).query(second['shape'])
            if np.min(distances_to_first) <= min(cfg.match_radius_m, cfg.spacing_m):
                relations.append({'first': first['loop']['id'], 'second': second['loop']['id'],
                                  'kind': 'geometric_contact', 'road_connectivity_verified': False})
    diagnostic_limits = list(limits)
    if crossings or len(groups) > 1:
        diagnostic_limits.append('crossing_connectivity_unresolved')
    report = {'kind': 'loop_recognition', 'input_points': len(coords), 'input_length_m': float(distances[-1]),
              'projection_epsg': epsg,
              'route_summary': _route_summary(coords, distances, gap, annotations, annotation_limits, cfg),
              'retrace_annotations': annotations,
              'whole_track': {'is_closed': gap <= cfg.closure_radius_m, 'start_end_distance_m': gap,
                              'tolerance_m': cfg.closure_radius_m},
              'has_loops': True if loops else (None if candidates or limits else False),
              'loops': loops, 'loop_candidates': candidates, 'closed_walks': closed_walks,
              'events': events, 'relations': relations,
              'excluded_excursions': ignored,
              'completeness': 'partial' if limits else 'complete',
              'completeness_scope': 'bounded_chronological_decomposition_at_reported_resolution_not_all_mathematical_cycles',
              'limitations': sorted(set(diagnostic_limits)),
              'resolution': {'spacing_m': cfg.spacing_m, 'return_radius_m': min(cfg.match_radius_m, cfg.spacing_m * .75),
                             'match_radius_m': cfg.match_radius_m, 'min_loop_length_m': cfg.min_loop_length_m,
                             'minimum_resolved_width_m': 2 * cfg.match_radius_m, 'analysis_samples': len(s)},
              'decomposition_method': 'continuous_source_returns_v2', 'road_identity_verified': False,
              'cautions': ['Only observed geometry is analyzed; no road identity or navigation guarantee.',
                           'No detections does not exclude loops below the reported resolution.',
                           'Crossings and proximity do not establish physical road connectivity.']}
    result = {'schema_version': '2.1', 'kind': 'loop_recognition', 'input_sha256': hashlib.sha256(raw).hexdigest(),
              'source_name': str(source_name), 'config': asdict(cfg), 'original': coords.tolist(), 'report': report}
    json.dumps(result, allow_nan=False)
    return result


def recognize_loops(path, config=None):
    cfg = _config(config)
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(cfg.max_input_bytes + 1)
    except OSError as exc:
        raise RecognitionError('无法读取 GPX。') from exc
    return recognize_bytes(raw, cfg, Path(path).name)


@dataclass(frozen=True)
class RouteShapeConfig:
    """Whole-route visual shape settings, independent of strict-loop settings."""
    max_input_bytes: int = 16 * 1024 * 1024
    max_points: int = 50_000
    max_candidate_pairs: int = 250_000
    max_graph_edges: int = 100_000
    max_face_steps: int = 400_000
    min_resolved_length_m: float = 80.
    ambiguous_corridor_width_m: float = 12.
    vertex_on_edge_tolerance_m: float = .05
    dominant_ring_share: float = .60
    secondary_ring_share: float = .15
    internal_structure_share: float = .12
    minimum_cyclic_share: float = .50
    corridor_secondary_ratio: float = .35
    corridor_main_share: float = .50
    corridor_bridge_share: float = .15
    significant_block_share: float = .08
    maximum_unresolved_share: float = .20
    maximum_crossing_share: float = .05
    crossing_neighborhood_m: float = 40.
    near_closure_radius_m: float = 25.
    near_closure_max_fraction: float = .01


class _ShapeFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _shape_config(config):
    if config is None:
        config = RouteShapeConfig()
    if type(config) is dict:
        try:
            config = RouteShapeConfig(**config)
        except TypeError as exc:
            raise RecognitionError('整体分类配置字段无效。') from exc
    if not isinstance(config, RouteShapeConfig):
        raise RecognitionError('整体分类配置类型无效。')
    defaults = RouteShapeConfig()
    for field in fields(config):
        value = getattr(config, field.name)
        if field.name.startswith('max_'):
            if type(value) is not int or not 1 <= value <= getattr(defaults, field.name):
                raise RecognitionError(f'{field.name} 超出允许的正整数上限。')
        elif type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise RecognitionError(f'{field.name} 必须是正的有限数值。')
    if not .001 <= config.vertex_on_edge_tolerance_m <= .2:
        raise RecognitionError('原始顶点在线段上的数值容差支持0.001–0.2米。')
    if not 20 <= config.min_resolved_length_m <= 100_000:
        raise RecognitionError('最小可分辨长度超出支持范围。')
    if not 1 <= config.ambiguous_corridor_width_m <= 30:
        raise RecognitionError('未分辨走廊宽度支持1–30米。')
    if not 1 <= config.crossing_neighborhood_m <= 200:
        raise RecognitionError('交叉影响邻域支持1–200米。')
    if not 0 < config.near_closure_radius_m <= 100:
        raise RecognitionError('整体近闭合半径支持0–100米（不含0）。')
    if any(not 0 < getattr(config, name) < 1 for name in (
            'dominant_ring_share', 'secondary_ring_share',
            'internal_structure_share', 'minimum_cyclic_share', 'corridor_secondary_ratio',
            'significant_block_share', 'maximum_unresolved_share', 'maximum_crossing_share',
            'corridor_main_share', 'corridor_bridge_share', 'near_closure_max_fraction')):
        raise RecognitionError('整体形态比例门槛必须介于0与1之间。')
    return config


def _observed_shape_graph(coords, xy, cfg):
    """Exact observation identities with bounded vertex-on-edge subdivision.

    No proximity clustering: two close roads stay distinct. Existing vertices
    may subdivide a geometrically identical straight segment, allowing different
    sampling densities without manufacturing arbitrary intersection junctions.
    """
    lookup, vertices, visits = {}, [], []
    for coord, point in zip(coords, xy):
        key = tuple(coord)
        if key not in lookup:
            lookup[key] = len(vertices)
            vertices.append(point)
        visits.append(lookup[key])
    vertices = np.asarray(vertices)
    tree = cKDTree(vertices)
    segments = {}
    for a, b in zip(visits[:-1], visits[1:]):
        if a != b:
            key = (min(a, b), max(a, b))
            segments[key] = segments.get(key, 0) + 1
    edges = {}
    incident = [[] for _ in vertices]
    for a, b in segments:
        incident[a].append(b)
        incident[b].append(a)
    budget = 0
    for (a, b), count in segments.items():
        delta = vertices[b] - vertices[a]
        length = float(np.linalg.norm(delta))
        midpoint = (vertices[a] + vertices[b]) / 2
        radius = length / 2 + cfg.vertex_on_edge_tolerance_m
        size = int(tree.query_ball_point(midpoint, radius, return_length=True))
        budget += size
        if budget > cfg.max_candidate_pairs:
            raise _ShapeFailure('resource_limit', '原始线段细分候选超过上限。')
        candidates = np.asarray(tree.query_ball_point(midpoint, radius), dtype=int)
        offset = vertices[candidates] - vertices[a]
        phase = offset @ delta / (length * length)
        residual = np.linalg.norm(offset - phase[:, None] * delta, axis=1)
        good = (phase > 1e-9) & (phase < 1 - 1e-9) & (residual <= cfg.vertex_on_edge_tolerance_m)
        middle = []
        for fraction, node in zip(phase[good], candidates[good]):
            # Subdivision may normalize a differently sampled SAME corridor.
            # A vertex merely lying on another path's crossing is not an
            # observed visit by that path: require incident collinear support.
            supported = False
            for neighbor in incident[node]:
                budget += 1
                if budget > cfg.max_candidate_pairs:
                    raise _ShapeFailure('resource_limit', '原始走廊细分证据超过上限。')
                vector = vertices[neighbor] - vertices[node]
                norm = float(np.linalg.norm(vector))
                if norm <= 1e-9:
                    continue
                sine = abs(float(delta[0] * vector[1] - delta[1] * vector[0])) / (length * norm)
                if sine <= min(.01, cfg.vertex_on_edge_tolerance_m / min(norm, length)):
                    supported = True
                    break
            if supported:
                middle.append((float(fraction), int(node)))
        middle.sort()
        chain = [a, *[node for _, node in middle], b]
        for u, v in zip(chain[:-1], chain[1:]):
            if u == v:
                continue
            key = (min(u, v), max(u, v))
            if key not in edges:
                edges[key] = [float(np.linalg.norm(vertices[v] - vertices[u])), 0]
            edges[key][1] += count
            if len(edges) > cfg.max_graph_edges:
                raise _ShapeFailure('resource_limit', '整体形态观测边超过上限。')
    pairs = list(edges)
    weights = np.asarray([edges[pair][0] for pair in pairs])
    counts = np.asarray([edges[pair][1] for pair in pairs])
    adjacency = [[] for _ in vertices]
    for edge, (a, b) in enumerate(pairs):
        adjacency[a].append((b, edge))
        adjacency[b].append((a, edge))
    return vertices, pairs, weights, counts, adjacency, budget


def _shape_blocks(adjacency):
    """Iterative Tarjan edge-biconnected blocks; never enumerate simple cycles."""
    discovery, low, parent = {}, {}, {}
    blocks, edge_stack = [], []
    clock = 0
    for root in range(len(adjacency)):
        if root in discovery:
            continue
        discovery[root] = low[root] = clock
        clock += 1
        stack = [(root, iter(adjacency[root]))]
        while stack:
            node, neighbors = stack[-1]
            try:
                other, edge = next(neighbors)
            except StopIteration:
                stack.pop()
                if node in parent:
                    previous, previous_edge = parent[node]
                    low[previous] = min(low[previous], low[node])
                    if low[node] >= discovery[previous]:
                        component = []
                        while edge_stack:
                            last = edge_stack.pop()
                            component.append(last)
                            if last == previous_edge:
                                break
                        blocks.append(component)
                continue
            if node in parent and edge == parent[node][1]:
                continue
            if other not in discovery:
                parent[other] = (node, edge)
                discovery[other] = low[other] = clock
                clock += 1
                edge_stack.append(edge)
                stack.append((other, iter(adjacency[other])))
            elif discovery[other] < discovery[node]:
                low[node] = min(low[node], discovery[other])
                edge_stack.append(edge)
    return blocks


def _block_boundary(block, vertices, pairs, weights, cfg, work):
    """Bounded geometric half-edge boundary of one observed cyclic block.

    This is a visual envelope, NOT a traversed occurrence or verified road face.
    Unnoded crossings are not turned into junctions. Self-crossing/degenerate
    envelopes are flagged by inconsistent signed faces, not silently polygonized.
    """
    neighbors = {}
    for edge in block:
        a, b = pairs[edge]
        neighbors.setdefault(a, []).append(b)
        neighbors.setdefault(b, []).append(a)
    for a in neighbors:
        neighbors[a].sort(key=lambda b: (math.atan2(vertices[b, 1] - vertices[a, 1],
                                                   vertices[b, 0] - vertices[a, 0]), b))
    successor = {}
    for node, ordered in neighbors.items():
        for index, previous in enumerate(ordered):
            successor[(previous, node)] = (node, ordered[(index - 1) % len(ordered)])
    seen, faces = set(), []
    for start in sorted(successor):
        if start in seen:
            continue
        edge, face = start, []
        while edge not in seen:
            work[0] += 1
            if work[0] > cfg.max_face_steps:
                raise _ShapeFailure('resource_limit', '整体形态边界计算超过上限。')
            seen.add(edge)
            face.append(edge[0])
            edge = successor[edge]
        if edge != start:
            raise _ShapeFailure('ambiguous_shape', '观测图边界无法一致闭合。')
        points = vertices[face]
        local = points - points[0]
        signed_area = float(np.sum(local[:, 0] * np.roll(local[:, 1], -1)
                                   - local[:, 1] * np.roll(local[:, 0], -1)) / 2)
        faces.append((signed_area, face))
    # Exterior is the clockwise (negative area) half-edge boundary.
    area, boundary = min(faces, key=lambda item: item[0])
    if abs(area) <= 1e-6:
        raise _ShapeFailure('ambiguous_shape', '环形部分退化或存在未分辨交叉。')
    boundary_set = {tuple(sorted((a, b))) for a, b in zip(boundary, boundary[1:] + boundary[:1])}
    perimeter = sum(weights[e] for e in block if pairs[e] in boundary_set)
    return {'edges': block, 'nodes': list(neighbors), 'boundary_nodes': boundary,
            'length_m': float(sum(weights[e] for e in block)), 'boundary_m': float(perimeter),
            'area_m2': abs(area), 'cycle_rank': len(block) - len(neighbors) + 1,
            'largest_face_area_m2': max((signed for signed, _ in faces), default=0.),
            'internal_share': max(0., 1 - perimeter / sum(weights[e] for e in block))}


def _canonical_shape_graph(vertices, pairs, weights, counts, adjacency, cfg):
    """Collapse only degree-two, same-count straight chains for visual analysis.

    A bounded iterative polyline simplification removes collinear recording
    points, so adjacency exemptions and crossing evidence have physical rather
    than input-segment scale. Junctions and genuine corners remain vertices.
    """
    visited, canonical, work = set(), [], 0
    for first_edge, (first_node, _) in enumerate(pairs):
        if first_edge in visited:
            continue
        # Prefer an actual junction as the beginning of an open chain.
        a, b = pairs[first_edge]
        start = a if len(adjacency[a]) != 2 else b
        path, path_edges = [start], []
        node, edge = start, first_edge
        while edge not in visited:
            visited.add(edge)
            path_edges.append(edge)
            u, v = pairs[edge]
            other = v if node == u else u
            path.append(other)
            if len(adjacency[other]) != 2 or other == start:
                break
            following = [(n, e) for n, e in adjacency[other] if e != edge]
            if counts[following[0][1]] != counts[edge]:
                break
            node, edge = other, following[0][1]
        # Open chains started in their middle also extend backwards.
        if len(adjacency[start]) == 2 and path[-1] != start:
            remaining = [e for _, e in adjacency[start] if e not in visited]
            if remaining and counts[remaining[0]] == counts[path_edges[0]]:
                reverse_path, reverse_edges = [start], []
                node, edge = start, remaining[0]
                while edge not in visited:
                    visited.add(edge)
                    reverse_edges.append(edge)
                    u, v = pairs[edge]
                    other = v if node == u else u
                    reverse_path.append(other)
                    if len(adjacency[other]) != 2:
                        break
                    next_edge = next(e for _, e in adjacency[other] if e != edge)
                    if counts[next_edge] != counts[edge]:
                        break
                    node, edge = other, next_edge
                path = reverse_path[:0:-1] + path
                path_edges = reverse_edges[::-1] + path_edges
        if path[0] == path[-1] and len(path) > 3:
            # A closed chain has no intrinsic first sample. Anchor at a genuine
            # corner, deterministically in geometry space, not at the first edge
            # supplied by the caller. Rotate edge provenance with the nodes.
            body = path[:-1]
            polygon = vertices[body]
            incoming = polygon - np.roll(polygon, 1, axis=0)
            outgoing = np.roll(polygon, -1, axis=0) - polygon
            cosines = np.sum(incoming * outgoing, axis=1) / np.maximum(
                np.linalg.norm(incoming, axis=1) * np.linalg.norm(outgoing, axis=1), 1e-12)
            anchor = min(range(len(body)), key=lambda k: (
                round(float(cosines[k]), 6), float(polygon[k, 0]), float(polygon[k, 1])))
            body = body[anchor:] + body[:anchor]
            path = body + body[:1]
            path_edges = path_edges[anchor:] + path_edges[:anchor]
        elif tuple(vertices[path[-1]]) < tuple(vertices[path[0]]):
            path.reverse()
            path_edges.reverse()
        keep = {0, len(path) - 1}
        pending = [(0, len(path) - 1)]
        while pending:
            lo, hi = pending.pop()
            if hi <= lo + 1:
                continue
            points = vertices[path[lo + 1:hi]]
            work += len(points)
            if work > cfg.max_face_steps:
                raise _ShapeFailure('resource_limit', '共线观测链归一化超过上限。')
            origin, end = vertices[path[lo]], vertices[path[hi]]
            vector = end - origin
            norm2 = float(vector @ vector)
            if norm2 <= 1e-12:
                errors = np.linalg.norm(points - origin, axis=1)
            else:
                phase = np.clip((points - origin) @ vector / norm2, 0, 1)
                errors = np.linalg.norm(points - origin - phase[:, None] * vector, axis=1)
            index = int(np.argmax(errors))
            if errors[index] > cfg.vertex_on_edge_tolerance_m:
                split = lo + 1 + index
                keep.add(split)
                pending.extend(((lo, split), (split, hi)))
        selected = sorted(keep)
        for lo, hi in zip(selected[:-1], selected[1:]):
            if path[lo] == path[hi]:
                continue
            canonical.append((path[lo], path[hi], float(sum(weights[e] for e in path_edges[lo:hi])),
                              int(counts[path_edges[lo]])))
    new_pairs = [(min(a, b), max(a, b)) for a, b, _, _ in canonical]
    new_weights = np.asarray([w for _, _, w, _ in canonical])
    new_counts = np.asarray([n for _, _, _, n in canonical])
    new_adj = [[] for _ in vertices]
    for edge, (a, b) in enumerate(new_pairs):
        new_adj[a].append((b, edge))
        new_adj[b].append((a, edge))
    return new_pairs, new_weights, new_counts, new_adj


def _narrow_edge_interval(origin, direction, length, other_origin, other_delta, other_length, width):
    """Clip an edge to the other segment's projection and signed-distance band."""
    axis = other_delta / other_length
    projection = float(np.dot(origin - other_origin, axis))
    slope = float(np.dot(direction, axis))
    normal = np.asarray([-axis[1], axis[0]])
    lateral = float(np.dot(origin - other_origin, normal))
    lateral_slope = float(np.dot(direction, normal))
    lo, hi = 0., length
    for value, rate, minimum, maximum in ((projection, slope, 0., other_length),
                                           (lateral, lateral_slope, -width, width)):
        if abs(rate) < 1e-12:
            if value < minimum or value > maximum:
                return None
        else:
            first, last = sorted(((minimum - value) / rate, (maximum - value) / rate))
            lo, hi = max(lo, first), min(hi, last)
            if hi <= lo:
                return None
    return lo, hi


def _crossing_chain_ranges(vertices, pairs, lengths, seeds, cfg):
    """Extend physical neighborhoods across artificial collinear edge breaks.

    Continue only through an unambiguous straight pair, never across a turn or
    into all branches of a junction. Overlapping ranges are unioned by caller.
    """
    incident = [[] for _ in vertices]
    for edge, (a, b) in enumerate(pairs):
        incident[a].append(edge)
        incident[b].append(edge)
    ranges, work = {}, 0
    pending = [(edge, position, cfg.crossing_neighborhood_m) for edge, position in seeds]
    best = {}
    while pending:
        edge, position, radius = pending.pop()
        key = (edge, round(position, 8))
        if radius <= best.get(key, -1.):
            continue
        best[key] = radius
        work += 1
        if work > cfg.max_face_steps:
            raise _ShapeFailure('resource_limit', '交叉物理链邻域传播超过上限。')
        ranges.setdefault(edge, []).append((max(0., position - radius),
                                            min(lengths[edge], position + radius)))
        a, b = pairs[edge]
        for node, available in ((a, radius - position), (b, radius - (lengths[edge] - position))):
            if available <= 1e-8:
                continue
            other_end = b if node == a else a
            incoming = vertices[node] - vertices[other_end]
            candidates = []
            for following in incident[node]:
                if following == edge:
                    continue
                work += 1
                if work > cfg.max_face_steps:
                    raise _ShapeFailure('resource_limit', '交叉物理链邻域传播超过上限。')
                u, v = pairs[following]
                target = v if node == u else u
                outgoing = vertices[target] - vertices[node]
                norm = float(np.linalg.norm(incoming) * np.linalg.norm(outgoing))
                if norm > 1e-12 and float(incoming @ outgoing) / norm > .9999:
                    candidates.append((following, 0. if node == u else float(lengths[following])))
            if len(candidates) == 1:
                following, at = candidates[0]
                pending.append((following, at, available))
    return ranges


def _shape_geometry_guard(vertices, pairs, cfg):
    """Reject unresolved crossings/close corridors instead of guessing topology.

    Exact shared observations are already nodes. This guard never inserts a
    connection at an unobserved crossing and never merges nearby parallel roads.
    """
    if not pairs:
        raise _ShapeFailure('ambiguous_shape', '观测几何在当前分辨率下退化，无法判断整体形态。')
    endpoints = np.asarray(pairs, dtype=int)
    first, last = vertices[endpoints[:, 0]], vertices[endpoints[:, 1]]
    delta = last - first
    lengths = np.linalg.norm(delta, axis=1)
    centers = (first + last) / 2
    half = lengths / 2
    tree = cKDTree(centers)
    budget = 0
    close_edges = {}
    crossing_events = []
    incident = {}
    for edge, (a, b) in enumerate(pairs):
        incident.setdefault(a, []).append((edge, b))
        incident.setdefault(b, []).append((edge, a))
    # Repeated sampling can land exactly on BOTH straight-through passes at a
    # crossing. Coordinate equality alone must not promote such a pass-through
    # to independently observed O/Q evidence.
    pass_through = set()
    for node, arms in incident.items():
        if len(arms) < 4:
            continue
        directions = np.asarray([vertices[b] - vertices[node] for _, b in arms])
        directions /= np.maximum(np.linalg.norm(directions, axis=1)[:, None], 1e-12)
        # Extra short branches cannot disable crossing evidence. Look for two
        # disjoint opposite direction pairs at any degree, using a bounded loop.
        opposite = []
        found = False
        for i in range(len(arms)):
            if found:
                break
            for j in range(i + 1, len(arms)):
                budget += 1
                if budget > cfg.max_candidate_pairs:
                    raise _ShapeFailure('resource_limit', '节点直穿方向核查超过上限。')
                if float(directions[i] @ directions[j]) >= -.995:
                    continue
                for a, b in opposite:
                    budget += 1
                    if budget > cfg.max_candidate_pairs:
                        raise _ShapeFailure('resource_limit', '节点直穿方向核查超过上限。')
                    if i not in (a, b) and j not in (a, b):
                        found = True
                        break
                if found:
                    break
                opposite.append((i, j))
        if found:
            pass_through.add(node)
    max_half = float(half.max())
    for i, center in enumerate(centers):
        radius = float(half[i] + max_half + cfg.ambiguous_corridor_width_m)
        size = int(tree.query_ball_point(center, radius, return_length=True))
        budget += size
        if budget > cfg.max_candidate_pairs:
            raise _ShapeFailure('resource_limit', '交叉与近邻走廊核查超过候选上限。')
        for j in tree.query_ball_point(center, radius):
            if j <= i:
                continue
            shared = set(pairs[i]) & set(pairs[j])
            if np.linalg.norm(center - centers[j]) > half[i] + half[j] + cfg.ambiguous_corridor_width_m:
                continue
            u, v = delta[i], delta[j]
            offset = first[j] - first[i]
            determinant = float(u[0] * v[1] - u[1] * v[0])
            if abs(determinant) > 1e-8 * lengths[i] * lengths[j]:
                t = float((offset[0] * v[1] - offset[1] * v[0]) / determinant)
                w = float((offset[0] * u[1] - offset[1] * u[0]) / determinant)
                within = -1e-7 <= t <= 1 + 1e-7 and -1e-7 <= w <= 1 + 1e-7
                interior = 1e-7 < t < 1 - 1e-7 or 1e-7 < w < 1 - 1e-7
                if within and interior and not shared:
                    t, w = float(np.clip(t, 0, 1)), float(np.clip(w, 0, 1))
                    crossing_events.append((i, j, t, w, first[i] + t * u))
            if abs(float(np.dot(u, v) / (lengths[i] * lengths[j]))) < .95:
                continue
            interval_i = _narrow_edge_interval(first[i], u / lengths[i], lengths[i],
                                               first[j], v, lengths[j], cfg.ambiguous_corridor_width_m)
            interval_j = _narrow_edge_interval(first[j], v / lengths[j], lengths[j],
                                               first[i], u, lengths[i], cfg.ambiguous_corridor_width_m)
            if interval_i is not None and interval_j is not None:
                lo_i, hi_i = interval_i
                lo_j, hi_j = interval_j
                # A shared node excuses only a physical neighborhood, not two
                # entire long arms of a narrow hairpin/triangle.
                for node in shared:
                    if node == pairs[i][0]:
                        lo_i = max(lo_i, cfg.ambiguous_corridor_width_m)
                    else:
                        hi_i = min(hi_i, lengths[i] - cfg.ambiguous_corridor_width_m)
                    if node == pairs[j][0]:
                        lo_j = max(lo_j, cfg.ambiguous_corridor_width_m)
                    else:
                        hi_j = min(hi_j, lengths[j] - cfg.ambiguous_corridor_width_m)
                if hi_i > lo_i:
                    close_edges.setdefault(i, []).append((lo_i, hi_i))
                if hi_j > lo_j:
                    close_edges.setdefault(j, []).append((lo_j, hi_j))
    overlap_length = 0.
    for intervals in close_edges.values():
        right = 0.
        for start, end in sorted(intervals):
            overlap_length += max(0., end - max(start, right))
            right = max(right, end)
    crossing_seeds = [(edge, phase * lengths[edge])
                      for i, j, t, w, _ in crossing_events for edge, phase in ((i, t), (j, w))]
    for node in pass_through:
        crossing_seeds.extend((edge, 0. if pairs[edge][0] == node else float(lengths[edge]))
                              for edge, _ in incident[node])
    crossing_ranges = _crossing_chain_ranges(vertices, pairs, lengths, crossing_seeds, cfg)
    crossing_length = 0.
    for ranges in crossing_ranges.values():
        right = 0.
        for start, end in sorted(ranges):
            crossing_length += max(0., end - max(right, start))
            right = max(right, end)
    return {'pairs': budget, 'near_corridor_length_m': overlap_length,
            'crossing_length_m': crossing_length, 'crossing_edges': len(crossing_ranges),
            'crossing_events': crossing_events, 'pass_through_nodes': pass_through}


def _interior_bridge_length(main, cyclic_edges, vertices, pairs, weights, cfg, work):
    """Measure bridge fragments inside the dominant visual boundary, not its Q stem."""
    if main is None:
        return 0.
    polygon = vertices[main['boundary_nodes']]
    a, b = polygon, np.roll(polygon, -1, axis=0)
    total = 0.
    for edge, (u, v) in enumerate(pairs):
        if edge in cyclic_edges:
            continue
        # Require both inner probes to be inside; a boundary departure itself
        # is not an internal branch, and no point is removed from the source.
        inside = True
        for fraction in (.25, .75):
            work[0] += len(polygon)
            if work[0] > cfg.max_face_steps:
                raise _ShapeFailure('resource_limit', '内部枝干位置核查超过上限。')
            point = vertices[u] * (1 - fraction) + vertices[v] * fraction
            crosses_y = (a[:, 1] > point[1]) != (b[:, 1] > point[1])
            selected_a, selected_b = a[crosses_y], b[crosses_y]
            crosses_x = (selected_a[:, 0] + (point[1] - selected_a[:, 1])
                         * (selected_b[:, 0] - selected_a[:, 0])
                         / (selected_b[:, 1] - selected_a[:, 1])) > point[0]
            if int(crosses_x.sum()) % 2 == 0:
                inside = False
                break
        if inside:
            total += weights[edge]
    return float(total)


def _split_visual_crossings(vertices, pairs, weights, counts, events, cfg):
    """Node the drawing for shape hypotheses only, never physical connectivity."""
    nodes = list(vertices)
    cuts = {}
    comparisons = 0
    for i, j, t, w, point in events:
        # The same crossing can be reported by several edges; merge numerical
        # duplicates deterministically without transitive proximity clustering.
        node = (pairs[i][0] if t <= 1e-7 else pairs[i][1] if t >= 1 - 1e-7 else
                pairs[j][0] if w <= 1e-7 else pairs[j][1] if w >= 1 - 1e-7 else None)
        for k in range(len(vertices), len(nodes)):
            if node is not None:
                break
            comparisons += 1
            if comparisons > cfg.max_candidate_pairs:
                raise _ShapeFailure('resource_limit', '视觉交叉去重比较超过上限。')
            if np.linalg.norm(nodes[k] - point) <= cfg.vertex_on_edge_tolerance_m:
                node = k
                break
        if node is None:
            node = len(nodes)
            nodes.append(point)
        cuts.setdefault(i, []).append((t, node))
        cuts.setdefault(j, []).append((w, node))
    edges = {}
    for edge, (a, b) in enumerate(pairs):
        chain = [(0., a), *sorted(cuts.get(edge, [])), (1., b)]
        for (start, u), (end, v) in zip(chain[:-1], chain[1:]):
            if u == v or end - start <= 1e-9:
                continue
            key = (min(u, v), max(u, v))
            if key not in edges:
                edges[key] = [float(weights[edge] * (end - start)), 0]
            edges[key][1] += int(counts[edge])
            if len(edges) > cfg.max_graph_edges:
                raise _ShapeFailure('resource_limit', '视觉交叉假设边数超过上限。')
    adjacency = [[] for _ in nodes]
    pairs = list(edges)
    for edge, (a, b) in enumerate(pairs):
        adjacency[a].append((b, edge))
        adjacency[b].append((a, edge))
    return (np.asarray(nodes), pairs, np.asarray([edges[key][0] for key in pairs]),
            np.asarray([edges[key][1] for key in pairs]), adjacency)


def _shape_metrics(coords, xy, cfg):
    vertices, pairs, weights, counts, adjacency, pair_work = _observed_shape_graph(coords, xy, cfg)
    pairs, weights, counts, adjacency = _canonical_shape_graph(vertices, pairs, weights, counts, adjacency, cfg)
    guard_work = _shape_geometry_guard(vertices, pairs, cfg)
    crossing_assumed = bool(guard_work['crossing_events'] or guard_work['pass_through_nodes'])
    # Independent O/Q support must survive removal of every geometrically
    # inferred crossing edge; a visual split alone cannot create its evidence.
    affected = {edge for i, j, *_ in guard_work['crossing_events'] for edge in (i, j)}
    affected.update(edge for edge, (a, b) in enumerate(pairs)
                    if a in guard_work['pass_through_nodes'] or b in guard_work['pass_through_nodes'])
    clean_adjacency = [[] for _ in vertices]
    for edge, (a, b) in enumerate(pairs):
        if edge not in affected:
            clean_adjacency[a].append((b, edge))
            clean_adjacency[b].append((a, edge))
    independent_length = max((sum(weights[e] for e in block)
                              for block in _shape_blocks(clean_adjacency) if len(block) > 1), default=0.)
    independent_share = float(independent_length / max(weights.sum(), 1e-9))
    if crossing_assumed:
        vertices, pairs, weights, counts, adjacency = _split_visual_crossings(
            vertices, pairs, weights, counts, guard_work['crossing_events'], cfg)
    total = float(weights.sum())
    if total <= 0:
        raise _ShapeFailure('invalid_input', '整体轨迹几何退化。')
    blocks, work = [], [0]
    for block in _shape_blocks(adjacency):
        if len(block) > 1:
            blocks.append(_block_boundary(block, vertices, pairs, weights, cfg, work))
    blocks.sort(key=lambda block: (-block['boundary_m'], -block['area_m2']))
    cyclic_edges = {edge for block in blocks for edge in block['edges']}
    cyclic = float(sum(weights[edge] for edge in cyclic_edges))
    main = blocks[0] if blocks else None
    main_length = main['boundary_m'] if main else 0.
    scale = max(float(np.linalg.norm(np.ptp(vertices, axis=0))), 1.)
    # Bounding-box extent is an auxiliary span measure, never sufficient evidence
    # of a ring. It penalizes little loops strung along a long corridor.
    ring_span = float(np.linalg.norm(np.ptp(vertices[main['nodes']], axis=0))) if main else 0.
    significant = [block for block in blocks if block['boundary_m'] >= max(cfg.min_resolved_length_m, cfg.significant_block_share * total)]
    secondary = max((block['boundary_m'] for block in blocks[1:]), default=0.)
    interior_bridges = _interior_bridge_length(main, cyclic_edges, vertices, pairs, weights, cfg, work)
    return {'unique_length_m': total, 'travel_length_m': float(np.sum(weights * counts)),
            'cyclic_unique_share': cyclic / total,
            'cyclic_travel_share': float(sum(weights[e] * counts[e] for e in cyclic_edges) / max(np.sum(weights * counts), 1e-9)),
            'dominant_boundary_share': main_length / total,
            'secondary_boundary_share': secondary / total,
            'main_internal_share': main['internal_share'] if main else 0.,
            'main_largest_face_share': min(1., main['largest_face_area_m2'] / main['area_m2']) if main else 0.,
            'interior_bridge_share': interior_bridges / total,
            'main_cycle_rank': main['cycle_rank'] if main else 0,
            'main_span_share': ring_span / scale,
            'significant_ring_blocks': len(significant),
            'ring_block_count': len(blocks), 'bridge_share': 1 - cyclic / total,
            'cycle_boundaries_m': [block['boundary_m'] for block in blocks[:32]],
            'cycle_areas_m2': [block['area_m2'] for block in blocks[:32]],
            'largest_bounded_face_area_m2': max((block['largest_face_area_m2'] for block in blocks), default=0.),
            'candidate_pairs': pair_work, 'geometry_guard_pairs': guard_work['pairs'],
            'near_corridor_share': min(1., guard_work['near_corridor_length_m'] / total),
            'unresolved_crossing_share': min(1., guard_work['crossing_length_m'] / total),
            'unresolved_crossing_edges': guard_work['crossing_edges'],
            'visual_crossing_hypothesis': crossing_assumed,
            'independent_ring_evidence': independent_share >= cfg.dominant_ring_share,
            'independent_ring_share': independent_share, 'boundary_steps': work[0],
            'graph_vertices': len(vertices), 'graph_edges': len(pairs)}


def _decide_route_shape(metrics, cfg):
    if metrics['near_corridor_share'] > cfg.maximum_unresolved_share:
        raise _ShapeFailure('ambiguous_shape', '未分辨的近邻走廊涉及显著行程，不能可靠解释整体形态。')
    complex_evidence = (metrics['secondary_boundary_share'] >= cfg.secondary_ring_share
                        or metrics['main_internal_share'] >= cfg.internal_structure_share
                        or metrics['interior_bridge_share'] >= cfg.internal_structure_share)
    if metrics['visual_crossing_hypothesis']:
        # A geometric split is a visual hypothesis, not a verified junction.
        # It may support an explicitly nonstandard drawing, never certify O/Q.
        if (complex_evidence and metrics['significant_ring_blocks'] >= 2
                and metrics['cyclic_unique_share'] >= cfg.minimum_cyclic_share):
            return 4, ['宏观双叶或多环自交构成非标准环形；交叉仅为视觉假设，未验证道路连通。']
        if not metrics['independent_ring_evidence']:
            raise _ShapeFailure('ambiguous_shape', '闭合主体依赖未观测交叉推导，不能据此认证为O/Q环。')
        if metrics['unresolved_crossing_share'] > cfg.maximum_crossing_share:
            raise _ShapeFailure('ambiguous_shape', '未分辨交叉影响整体解释，请人工核对。')
    if metrics['ring_block_count'] == 0:
        return 3, ['观测结构以开放路径或往返走廊为主，未形成可分辨闭合主体。']
    if max(metrics['cycle_boundaries_m']) < cfg.min_resolved_length_m:
        return 3, ['仅有低于宏观形态尺度的局部闭合，整体仍由路径或走廊构成。']
    if (metrics['dominant_boundary_share'] >= cfg.dominant_ring_share
            and metrics['secondary_boundary_share'] < cfg.secondary_ring_share
            and metrics['main_internal_share'] < cfg.internal_structure_share
            and metrics['interior_bridge_share'] < cfg.internal_structure_share):
        return 2, ['单一O/Q形闭合主体占主导，其他行程为次要附属结构。']
    if metrics['cyclic_unique_share'] < cfg.minimum_cyclic_share:
        return 3, ['线状或往返走廊占主导，局部闭合结构不足以代表整体。']
    main_share = metrics['dominant_boundary_share']
    secondary_ratio = metrics['secondary_boundary_share'] / max(main_share, 1e-9)
    if main_share < cfg.corridor_main_share and (metrics['significant_ring_blocks'] >= 3
                           or (metrics['bridge_share'] > cfg.corridor_bridge_share
                               and secondary_ratio < cfg.corridor_secondary_ratio)):
        return 3, ['局部环分散于桥接走廊，缺少主导整体的闭合主体或集中双环结构。']
    if complex_evidence:
        return 4, ['闭合或多环是明显构建特征，且存在显著次环或内部连接，非单一O/Q形。']
    raise _ShapeFailure('ambiguous_shape', '单环与外接走廊主导程度处于边界，未发现非标准结构，需人工核对。')


def _classification_result(category, reasons, cfg, metrics=None, reason_code=None, input_hash=None):
    result = {'schema_version': '1.0', 'kind': 'route_shape_classification',
              'category': category, 'label': {1: '识别失败', 2: '环', 3: '非环', 4: '非标准环'}[category],
              'status': 'failed' if category == 1 else 'classified',
              'method': 'overall_route_shape_v1', 'reasons': reasons,
              'metrics': metrics or {}, 'config': asdict(cfg),
              'limitations': ['geometry_only_not_verified_road_topology',
                              'exact_observation_graph_with_bounded_collinear_subdivision']}
    if reason_code:
        result['reason_code'] = reason_code
    if input_hash:
        result['input_sha256'] = input_hash
    return result


def _visual_near_closure(coords, xy, distances, cfg):
    """Conservative endpoint-only visual hypothesis; never alter input/occurrences."""
    gap = float(np.linalg.norm(xy[-1] - xy[0]))
    if gap <= 1e-8:
        return coords, xy, False, gap
    if gap > min(cfg.near_closure_radius_m, cfg.near_closure_max_fraction * distances[-1]):
        return coords, xy, False, gap
    # Collapse zero-length repeats only when counting actual endpoint visits.
    # Repeated interior visits to either endpoint make a one-observation snap
    # ambiguous; reject symmetrically rather than move whichever end is last.
    distinct = coords[np.r_[True, np.any(coords[1:] != coords[:-1], axis=1)]]
    if (np.sum(np.all(distinct == coords[0], axis=1)) != 1
            or np.sum(np.all(distinct == coords[-1], axis=1)) != 1):
        raise _ShapeFailure('ambiguous_shape', '近闭合端点身份在内部重复出现，不能建立方向无关的独立端点假设。')
    # Only join the two actual degree-one ends, not a nearby interior junction.
    neighbors = {}
    for a, b in zip(map(tuple, coords[:-1]), map(tuple, coords[1:])):
        if a != b:
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)
    if len(neighbors.get(tuple(coords[0]), ())) != 1 or len(neighbors.get(tuple(coords[-1]), ())) != 1:
        return coords, xy, False, gap
    # Original edges can hide an endpoint lying inside another visited segment.
    # Check the normalized corridor graph as well before joining identities.
    vertices, _, _, _, adjacency, _ = _observed_shape_graph(coords, xy, cfg)
    for point in (xy[0], xy[-1]):
        node = int(np.flatnonzero(np.all(vertices == point, axis=1))[0])
        if len(adjacency[node]) != 1:
            raise _ShapeFailure('ambiguous_shape', '近闭合端点在细分后并非独立开放端点。')
    nonzero = np.flatnonzero(np.linalg.norm(np.diff(xy, axis=0), axis=1) > 1e-7)
    first = xy[nonzero[0] + 1] - xy[nonzero[0]]
    last = xy[nonzero[-1] + 1] - xy[nonzero[-1]]
    heading = float(np.dot(first, last) / (np.linalg.norm(first) * np.linalg.norm(last)))
    if heading < -.8:
        # Near-return on reverse-aligned arms may be offset out-and-back data.
        return coords, xy, False, gap
    view_coords, view_xy = coords.copy(), xy.copy()
    # Symmetric internal endpoint identity. The original observations are never
    # edited, and reversing the route produces the identical geometry hypothesis.
    midpoint_coord = (coords[0] + coords[-1]) / 2
    midpoint_xy = (xy[0] + xy[-1]) / 2
    view_coords[[0, -1]] = midpoint_coord
    view_xy[[0, -1]] = midpoint_xy
    return view_coords, view_xy, True, gap


def _input_failure(exc, cfg, digest=None):
    message = str(exc)
    code = ('resource_limit' if ('超过' in message or ('超出' in message and '点数' in message)) else
            'unsupported_input' if ('支持' in message or 'UTM' in message) else 'invalid_input')
    return _classification_result(1, [message], cfg, reason_code=code, input_hash=digest)


def classify_route_bytes(raw, config=None):
    """Return the visual whole-route class, separately from strict loop reports."""
    cfg = _shape_config(config)
    if not isinstance(raw, bytes):
        return _classification_result(1, ['GPX输入必须为字节快照。'], cfg, reason_code='invalid_input')
    if len(raw) > cfg.max_input_bytes:
        return _classification_result(1, ['GPX输入超过字节上限。'], cfg, reason_code='resource_limit')
    digest = hashlib.sha256(raw).hexdigest()
    try:
        admission = LoopRecognitionConfig(max_input_bytes=cfg.max_input_bytes, max_points=cfg.max_points)
        coords, xy, distances, _ = _load(raw, admission)
    except RecognitionError as exc:
        return _input_failure(exc, cfg, digest)
    return _classify_prepared(coords, xy, distances, cfg, digest)


def classify_coordinates(coords, config=None):
    """Classify explicit [longitude, latitude] coordinates without GPX transcoding."""
    cfg = _shape_config(config)
    try:
        coords, xy, distances, _ = _prepare_coordinates(coords, cfg)
    except (RecognitionError, ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, RecognitionError):
            return _input_failure(exc, cfg)
        return _classification_result(1, ['路线坐标格式无效。'], cfg, reason_code='invalid_input')
    return _classify_prepared(coords, xy, distances, cfg)


def _ordered_return_share(xy, distances, width):
    """Measure the weaker arm of an out-and-back traversal by arc length."""
    sample_count = min(1501, max(51, int(distances[-1] / (width / 2)) + 1))
    grid = np.linspace(0, distances[-1], sample_count)
    points = np.column_stack([np.interp(grid, distances, axis) for axis in xy.T])
    best = 0.
    turns = np.unique(np.rint(np.linspace(.35, .65, 61) * (sample_count - 1)).astype(int))
    for turn in turns:
        outbound, inbound = points[:turn + 1], points[turn:][::-1]
        distance_to_inbound, inbound_indices = cKDTree(inbound).query(outbound)
        distance_to_outbound, outbound_indices = cKDTree(outbound).query(inbound)
        shares = []
        for arm, other, gaps, indices in (
                (outbound, inbound, distance_to_inbound, inbound_indices),
                (inbound, outbound, distance_to_outbound, outbound_indices)):
            # Compare short chords oriented toward the turn on both arms.
            arm_chords = arm[3:] - arm[:-3]
            other_chords = other[indices[3:]] - other[indices[:-3]]
            scales = np.linalg.norm(arm_chords, axis=1) * np.linalg.norm(other_chords, axis=1)
            aligned = np.sum(arm_chords * other_chords, axis=1) >= .8 * scales
            matched = ((gaps[:-3] <= width) & (gaps[3:] <= width)
                       & (indices[3:] > indices[:-3]) & (scales > 0) & aligned)
            shares.append(float(np.mean(matched)))
        best = max(best, min(shares))
    return best


def _outline_area(points):
    next_points = np.roll(points, -1, axis=0)
    return abs(float(np.sum(points[:, 0] * next_points[:, 1]
                            - points[:, 1] * next_points[:, 0]))) / 2


def _repeated_macro_ring(outline, hull, extent):
    """Compare complete revolutions at equal angular phases around the outline."""
    centered = outline - np.mean(outline[hull.vertices], axis=0)
    angle = np.unwrap(np.arctan2(centered[:, 1], centered[:, 0]))
    turn = angle[-1] - angle[0]
    phase = (angle - angle[0]) * np.sign(turn) / (2 * np.pi)
    laps = int(phase[-1])
    if laps < 2 or abs(turn) < .8 * float(np.sum(np.abs(np.diff(angle)))):
        return False
    advancing = np.r_[True, phase[1:] > np.maximum.accumulate(phase)[:-1]]
    phases = phase[advancing]
    if np.interp(laps, phases, np.flatnonzero(advancing)) < .60 * (len(outline) - 1):
        return False

    previous = None
    for lap in range(laps):
        grid = np.linspace(lap, lap + 1, 65)
        current = np.column_stack([np.interp(grid, phases, axis[advancing]) for axis in outline.T])
        if _outline_area(current) < .35 * hull.volume:
            return False
        if previous is not None and np.quantile(np.linalg.norm(current - previous, axis=1), .85) > .12 * extent:
            return False
        previous = current
    return True


def _macro_shape(xy, distances, cfg, sample_count):
    """Evaluate the ordered outline at a coarser scale than individual fixes."""
    samples = np.linspace(0, distances[-1], sample_count)
    outline = np.column_stack([np.interp(samples, distances, axis) for axis in xy.T])
    outline -= outline[0].copy()
    try:
        hull = ConvexHull(outline)
    except QhullError:
        return None, {}
    extent = float(np.max(pdist(outline[hull.vertices])))
    area = _outline_area(outline)
    gap_share = float(np.linalg.norm(outline[-1])) / extent
    area_share = area / hull.volume
    summary = {'endpoint_gap_share': gap_share, 'signed_area_share': area_share}
    if hull.volume / extent**2 < .06:
        return None, summary
    if area_share >= 1. and _repeated_macro_ring(outline, hull, extent):
        summary['repeated_ring'] = True
        return 2, summary
    if gap_share > .20:
        return None, summary
    closed = np.vstack([outline, outline[0]])
    # The closing chord belongs only to this visual outline.
    graph = _shape_metrics(closed, closed, cfg)
    main = graph['dominant_boundary_share']
    secondary = graph['secondary_boundary_share']
    block_areas = sorted(graph['cycle_areas_m2'], reverse=True)
    main_area, secondary_area = ([area / hull.volume for area in block_areas] + [0., 0.])[:2]
    largest_face = graph['largest_bounded_face_area_m2'] / hull.volume
    summary.update(dominant_boundary_share=main, secondary_boundary_share=secondary,
                   largest_block_area_share=main_area, second_block_area_share=secondary_area,
                   main_largest_face_share=graph['main_largest_face_share'],
                   largest_bounded_face_area_share=largest_face)
    if (gap_share <= .15 and secondary_area >= .08
            and secondary >= cfg.secondary_ring_share and secondary >= .5 * main
            and graph['cyclic_unique_share'] >= cfg.minimum_cyclic_share):
        return 4, summary
    # Interior mileage can be large when two nearby passes enclose a sliver.
    # Keep the chord guard when an interior division encloses meaningful area.
    narrow_internal_tracks = (graph['main_largest_face_share'] >= .92
                              and graph['near_corridor_share'] >= graph['main_internal_share'])
    if (gap_share <= .15 and area_share >= .40 and main_area >= .40
            and main >= .75 * cfg.dominant_ring_share
            and secondary_area < .08
            and (graph['main_internal_share'] < cfg.internal_structure_share or narrow_internal_tracks)
            and graph['interior_bridge_share'] < cfg.internal_structure_share):
        return 2, summary
    # The exterior of a fragmented block can enclose multiple thin slivers.
    # Check the largest individual region as well as net enclosure.
    if area_share <= .10 and largest_face < .08:
        return 3, summary
    return None, summary


def _classify_prepared(coords, xy, distances, cfg, digest=None):
    local = _classify_local_shape(coords, xy, distances, cfg, digest)
    metrics = local['metrics']
    sample_counts = (129, 257)
    # Require separation between the outline sampling scale and GPS corridor scale.
    if (local['category'] == 2
            or (not metrics and local.get('reason_code') != 'resource_limit')
            or np.linalg.norm(xy[-1] - xy[0]) <= 1e-8
            or distances[-1] < (sample_counts[-1] - 1) * cfg.ambiguous_corridor_width_m):
        return local
    candidates = []
    outlines = {}
    try:
        for count in sample_counts:
            category, summary = _macro_shape(xy, distances, cfg, count)
            candidates.append(category)
            outlines[str(count)] = summary
    except _ShapeFailure:
        return local
    metrics['macro_outlines'] = outlines
    category = candidates[0]
    # An open route with only small local cycles can remain non-ring even
    # when a hypothetical closing chord would enclose substantial area.
    if (local['category'] == 1 and all(candidate is None for candidate in candidates)
            and all(outline.get('endpoint_gap_share', 0.) > .15 for outline in outlines.values())
            and metrics.get('dominant_boundary_share', 1.) < cfg.significant_block_share
            and metrics['cyclic_unique_share'] < cfg.minimum_cyclic_share
            and metrics['unresolved_crossing_share'] <= cfg.maximum_crossing_share
            and metrics['near_corridor_share'] <= cfg.maximum_unresolved_share):
        return _classification_result(
            3, ['两种整体尺度下首尾均明显开放，原始轨迹只有局部小环和少量交叉。'],
            cfg, metrics, input_hash=digest)
    if category is None or category != candidates[1]:
        return local
    if category == 3 and (local['category'] != 1
                          or metrics.get('near_corridor_share', 0.) <= cfg.maximum_unresolved_share):
        return local
    reasons = {
        2: '两种整体采样尺度下均形成有明显围合面积的主导环边界。',
        3: '两种整体采样尺度下的净围合面积均很小，且存在显著近邻走廊。',
        4: '两种整体采样尺度下均存在两个显著的闭合主体。',
    }
    if category == 2 and all(outline.get('repeated_ring') for outline in outlines.values()):
        reasons[2] = '两种整体采样尺度下均沿同一个闭合主体同向绕行多圈。'
    return _classification_result(category, [reasons[category]], cfg, metrics, input_hash=digest)


def _classify_local_shape(coords, xy, distances, cfg, digest=None):
    metrics = {}
    try:
        admission = LoopRecognitionConfig(max_input_bytes=cfg.max_input_bytes, max_points=cfg.max_points)
        if distances[-1] < cfg.min_resolved_length_m:
            raise _ShapeFailure('ambiguous_shape', '路线低于整体形态最小可分辨尺度。')
        if _noise_diagnostic(xy, admission):
            raise _ShapeFailure('ambiguous_shape', '频繁小尺度反转使整体形态无法可靠判断。')
        view_coords, view_xy, near_closure, endpoint_gap = _visual_near_closure(coords, xy, distances, cfg)
        metrics = _shape_metrics(view_coords, view_xy, cfg)
        metrics['original_length_m'] = float(distances[-1])
        metrics['near_closure_assumed'] = near_closure
        metrics['endpoint_gap_m'] = endpoint_gap
        category, reasons = _decide_route_shape(metrics, cfg)
        # Test the same geometry at narrower/wider numerical identity tolerances.
        # This is bounded (three graph passes), not a road-map hypothesis search.
        # Keep all rule thresholds fixed: instability means category 1, never a
        # per-file threshold adjustment to reproduce a desired label.
        tested_tolerances = [cfg.vertex_on_edge_tolerance_m]
        for factor in (.5, 2.):
            tolerance = min(.2, max(.001, cfg.vertex_on_edge_tolerance_m * factor))
            if tolerance in tested_tolerances:
                continue
            tested_tolerances.append(tolerance)
            alternate = replace(cfg, vertex_on_edge_tolerance_m=tolerance)
            alternate_metrics = _shape_metrics(view_coords, view_xy, alternate)
            alternate_category, _ = _decide_route_shape(alternate_metrics, alternate)
            if alternate_category != category:
                return _classification_result(
                    1, ['不同合理几何分辨率下整体类别不稳定，请人工核对。'], cfg,
                    metrics, reason_code='ambiguous_shape', input_hash=digest)
        metrics['stability_tolerances_m'] = tested_tolerances
        if (category == 3 and 0 < endpoint_gap <= min(
                cfg.near_closure_radius_m, cfg.near_closure_max_fraction * distances[-1])
                and not np.array_equal(coords[0], coords[-1])):
            return _classification_result(
                1, ['首尾近闭合但未能建立可靠闭合主体，不能确定判为非环。'], cfg,
                metrics, reason_code='ambiguous_shape', input_hash=digest)
        if near_closure:
            reasons = [*reasons, '整体形态采用有界首尾近闭合假设；未修改原始轨迹或严格环记录。']
        return _classification_result(category, reasons, cfg, metrics, input_hash=digest)
    except _ShapeFailure as exc:
        if (exc.code == 'ambiguous_shape'
                and metrics.get('endpoint_gap_m', 0.) > cfg.ambiguous_corridor_width_m
                and (metrics['near_corridor_share'] > cfg.maximum_unresolved_share
                     or metrics['visual_crossing_hypothesis'])):
            share = _ordered_return_share(xy, distances, cfg.ambiguous_corridor_width_m)
            metrics['ordered_return_share'] = share
            if share >= .70:
                return _classification_result(
                    3, ['折返点两侧的大部分行程沿同一近邻走廊反向重合，整体为往返路线。'],
                    cfg, metrics, input_hash=digest)
            # A wider comparison can bridge GPS drift when the graph already
            # shows extensive close corridors and lacks a dominant boundary.
            wider_width = min(30., 2 * cfg.ambiguous_corridor_width_m)
            if (cfg.ambiguous_corridor_width_m < wider_width < metrics['endpoint_gap_m']
                    and metrics['near_corridor_share'] > cfg.maximum_unresolved_share
                    and metrics['dominant_boundary_share'] < cfg.corridor_main_share):
                wider_share = _ordered_return_share(xy, distances, wider_width)
                metrics['wider_ordered_return_share'] = wider_share
                metrics['wider_ordered_return_width_m'] = wider_width
                if wider_share >= .70:
                    return _classification_result(
                        3, ['近邻走廊覆盖显著且没有主导环边界；放宽定位偏移后，两侧行程仍有序反向重合。'],
                        cfg, metrics, input_hash=digest)
        return _classification_result(1, [str(exc)], cfg, metrics, reason_code=exc.code, input_hash=digest)
    except RecognitionError as exc:
        return _input_failure(exc, cfg, digest)


def classify_route(path, config=None):
    cfg = _shape_config(config)
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(cfg.max_input_bytes + 1)
    except OSError:
        return _classification_result(1, ['无法读取GPX输入。'], cfg, reason_code='invalid_input')
    return classify_route_bytes(raw, cfg)


MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_EXPORT_RECORD_BYTES = 16 * 1024 * 1024
MAX_EXPORT_RECORDS = 10_000


class ExportError(ValueError):
    """Safe, non-private JSON export/selection error."""


class BatchCancelled(Exception):
    """No full run was authorized; no result should be published."""


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ExportError('JSON包含重复字段。')
            result[key] = value
        return result

    def constant(_):
        raise ExportError('JSON不支持NaN或Infinity。')

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ExportError('JSON不支持超出有限范围的数值。')
        return value

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except ExportError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ExportError('JSON语法、编码或嵌套深度无效。') from exc


def _source_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _record_metadata(record, index, line=None):
    identifier = record.get('topRouteId') if isinstance(record, dict) else None
    warnings = []
    if not isinstance(identifier, str) or not identifier.strip():
        identifier = None
        warnings.append('missing_or_invalid_topRouteId')
    segments = record.get('segments') if isinstance(record, dict) else None
    points = (segments[0].get('routePoints') if isinstance(segments, list) and len(segments) == 1
              and isinstance(segments[0], dict) else None)
    return {'source_index': index, 'source_line': line, 'topRouteId': identifier,
            'point_count': len(points) if isinstance(points, list) else 0,
            'schema_supported': isinstance(points, list) and len(points) >= 3,
            'warnings': warnings}


class RouteExportIndex:
    """Bounded input index. NDJSON keeps offsets/hashes, never all coordinates."""
    def __init__(self, path, input_format='auto'):
        self.path = Path(path)
        self.entries = []
        self._document_records = None
        if input_format not in ('auto', 'json', 'ndjson'):
            raise ExportError('不支持的JSON导出格式。')
        try:
            with self.path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                if stat.st_size > MAX_EXPORT_BYTES:
                    raise ExportError('导出文件超过256MiB扫描上限。')
                self.signature = _source_signature(stat)
                first = b''
                first_offset = 0
                while True:
                    first_offset = stream.tell()
                    first = stream.readline(MAX_EXPORT_RECORD_BYTES + 1)
                    if not first:
                        raise ExportError('导出文件为空。')
                    if first_offset == 0:
                        first = first.removeprefix(b'\xef\xbb\xbf')
                    if first.strip():
                        break
                prefix = first.lstrip()
                if input_format == 'ndjson':
                    document = False
                elif prefix.startswith(b'['):
                    document = True
                else:
                    try:
                        _strict_json(first.decode('utf-8-sig'))
                        document = False
                    except ExportError:
                        document = True
                stream.seek(0)
                if document:
                    if stat.st_size > MAX_JSON_DOCUMENT_BYTES:
                        raise ExportError('单JSON文档超过64MiB上限，请使用NDJSON。')
                    raw = stream.read(MAX_JSON_DOCUMENT_BYTES + 1)
                    if len(raw) > MAX_JSON_DOCUMENT_BYTES:
                        raise ExportError('单JSON文档超过64MiB上限。')
                    data = _strict_json(raw.decode('utf-8-sig'))
                    records = data if isinstance(data, list) else [data]
                    if not records or len(records) > MAX_EXPORT_RECORDS:
                        raise ExportError('导出路线数量必须为1至10000。')
                    self.format = 'json_array' if isinstance(data, list) else 'json_object'
                    self._document_records = []
                    for index, record in enumerate(records, 1):
                        try:
                            encoded = json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
                        except (ValueError, TypeError, RecursionError) as exc:
                            raise ExportError('JSON路线记录无法安全编码。') from exc
                        if len(encoded) > MAX_EXPORT_RECORD_BYTES:
                            raise ExportError('单条路线记录超过16MiB上限。')
                        self._document_records.append(encoded)
                        self.entries.append(_record_metadata(record, index))
                    self.source_sha256 = hashlib.sha256(raw).hexdigest()
                else:
                    self.format = 'ndjson'
                    digest, line_number, total = hashlib.sha256(), 0, 0
                    while True:
                        offset = stream.tell()
                        raw = stream.readline(MAX_EXPORT_RECORD_BYTES + 1)
                        if not raw:
                            break
                        line_number += 1
                        total += len(raw)
                        if len(raw) > MAX_EXPORT_RECORD_BYTES or total > MAX_EXPORT_BYTES:
                            raise ExportError('NDJSON文件或单行记录超过大小上限。')
                        digest.update(raw)
                        text = raw.decode('utf-8-sig' if offset == 0 else 'utf-8')
                        if not text.strip():
                            continue
                        record = _strict_json(text)
                        if len(self.entries) >= MAX_EXPORT_RECORDS:
                            raise ExportError('导出路线数量超过10000上限。')
                        entry = _record_metadata(record, len(self.entries) + 1, line_number)
                        entry.update(offset=offset, byte_length=len(raw), record_sha256=hashlib.sha256(raw).hexdigest())
                        self.entries.append(entry)
                    if not self.entries:
                        raise ExportError('没有可选择的路线记录。')
                    self.source_sha256 = digest.hexdigest()
                if self.signature != _source_signature(os.fstat(stream.fileno())):
                    raise ExportError('索引期间源文件发生变化。')
        except ExportError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise ExportError('无法安全读取JSON导出文件。') from exc
        counts = {}
        for entry in self.entries:
            identifier = entry['topRouteId']
            if identifier is not None:
                counts[identifier] = counts.get(identifier, 0) + 1
        for entry in self.entries:
            if entry['topRouteId'] is not None and counts[entry['topRouteId']] > 1:
                entry['warnings'].append('duplicate_topRouteId')

    def select(self, *, top_route_id=None, start=None, count=None, all_routes=False):
        choices = int(top_route_id is not None) + int(start is not None or count is not None) + int(all_routes)
        if choices != 1:
            raise ExportError('JSON必须且只能选择ID、start/count或all。')
        if top_route_id is not None:
            if not isinstance(top_route_id, str) or not top_route_id.strip():
                raise ExportError('topRouteId必须为非空字符串。')
            found = [entry for entry in self.entries if entry['topRouteId'] == top_route_id]
            if len(found) != 1:
                raise ExportError('topRouteId未找到或存在重复，不能唯一选择。')
            return found
        if all_routes:
            return list(self.entries)
        if type(start) is not int or type(count) is not int or start < 1 or count < 1:
            raise ExportError('start和count必须同时提供且为正整数，序号从1开始。')
        if start + count - 1 > len(self.entries):
            raise ExportError('选择范围超出路线记录数量。')
        return self.entries[start - 1:start - 1 + count]

    def verify(self, full_hash=False):
        try:
            with self.path.open('rb') as stream:
                if self.signature != _source_signature(os.fstat(stream.fileno())):
                    raise ExportError('源文件已变化，请重新扫描和确认。')
                if full_hash:
                    digest, size = hashlib.sha256(), 0
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_EXPORT_BYTES:
                            raise ExportError('源文件已变化，请重新扫描和确认。')
                        digest.update(block)
                    if digest.hexdigest() != self.source_sha256:
                        raise ExportError('源文件内容已变化，请重新扫描和确认。')
        except OSError as exc:
            raise ExportError('源文件不可用，请重新扫描。') from exc

    def load(self, entry):
        self.verify()
        if self._document_records is not None:
            return _strict_json(self._document_records[entry['source_index'] - 1])
        try:
            with self.path.open('rb') as stream:
                if self.signature != _source_signature(os.fstat(stream.fileno())):
                    raise ExportError('源文件已变化，请重新扫描。')
                stream.seek(entry['offset'])
                raw = stream.read(entry['byte_length'])
            if hashlib.sha256(raw).hexdigest() != entry['record_sha256']:
                raise ExportError('选中路线内容已变化，请重新扫描。')
            return _strict_json(raw.decode('utf-8-sig' if entry['offset'] == 0 else 'utf-8'))
        except (OSError, UnicodeError) as exc:
            raise ExportError('无法读取选中路线记录。') from exc


def _valid_export_numbers(lon, lat, altitude):
    try:
        finite = lambda value: (type(value) in (int, float) and math.isfinite(value))
        valid = finite(lon) and finite(lat) and (altitude is None or finite(altitude))
    except (OverflowError, ValueError):
        valid = False
    return valid


def classify_export_record(record, config=None):
    """Adapt one exported route to the same coordinate classifier used by GPX."""
    cfg = _shape_config(config)
    if not isinstance(record, dict):
        return _classification_result(1, ['路线记录必须是对象。'], cfg, reason_code='invalid_input')
    segments = record.get('segments')
    if not isinstance(segments, list) or not segments:
        return _classification_result(1, ['路线缺少segments。'], cfg, reason_code='invalid_input')
    if len(segments) != 1:
        return _classification_result(1, ['首版不支持多segment，不会跨段连接。'], cfg, reason_code='unsupported_input')
    points = segments[0].get('routePoints') if isinstance(segments[0], dict) else None
    if not isinstance(points, list) or not 3 <= len(points) <= cfg.max_points:
        return _classification_result(1, ['routePoints为空或点数超出限制。'], cfg,
                                      reason_code='resource_limit' if isinstance(points, list) and len(points) > cfg.max_points else 'invalid_input')
    coords = []
    for point in points:
        if not isinstance(point, dict):
            return _classification_result(1, ['路线点必须为对象。'], cfg, reason_code='invalid_input')
        lon, lat, altitude = point.get('longitude'), point.get('latitude'), point.get('altitude')
        if not _valid_export_numbers(lon, lat, altitude):
            return _classification_result(1, ['经纬度和可选海拔必须为有限数值。'], cfg, reason_code='invalid_input')
        coords.append([lon, lat])
    return classify_coordinates(coords, cfg)


def _worker_main():
    """Private bounded data-only subprocess protocol; stdout contains one result."""
    try:
        raw = sys.stdin.buffer.read(MAX_EXPORT_RECORD_BYTES + 1)
        if len(raw) > MAX_EXPORT_RECORD_BYTES:
            raise ExportError('worker输入超过上限。')
        request = _strict_json(raw)
        result = classify_export_record(request['record'], request.get('config'))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception:
        # Never echo untrusted data, metadata or private exception paths.
        print(json.dumps({'worker_error': 'internal_error'}))
        return 2


def _run_route_worker(record, config, timeout, heartbeat=None):
    # Admit and minimize data before transport; descriptions and point metadata
    # cannot inflate the request or cross the worker boundary.
    if not isinstance(record, dict):
        return _classification_result(1, ['路线记录必须是对象。'], config, reason_code='invalid_input')
    segments = record.get('segments')
    if not isinstance(segments, list) or not segments:
        return _classification_result(1, ['路线缺少segments。'], config, reason_code='invalid_input')
    if len(segments) != 1:
        return _classification_result(1, ['首版不支持多segment，不会跨段连接。'], config, reason_code='unsupported_input')
    points = segments[0].get('routePoints') if isinstance(segments[0], dict) else None
    if not isinstance(points, list) or not 3 <= len(points) <= config.max_points:
        return _classification_result(1, ['路线点数无效或超过上限。'], config,
            reason_code='resource_limit' if isinstance(points, list) and len(points) > config.max_points else 'invalid_input')
    minimal = []
    for point in points:
        if not isinstance(point, dict) or not _valid_export_numbers(
                point.get('longitude'), point.get('latitude'), point.get('altitude')):
            return _classification_result(1, ['路线点必须包含有限数值坐标。'], config, reason_code='invalid_input')
        item = {key: point[key] for key in ('longitude', 'latitude', 'altitude') if key in point}
        minimal.append(item)
    geometry = {'segments': [{'routePoints': minimal}]}
    request = json.dumps({'record': geometry, 'config': asdict(config)}, ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
    if len(request) > MAX_EXPORT_RECORD_BYTES:
        return _classification_result(1, ['worker输入超过上限。'], config, reason_code='resource_limit')
    # Windows communicate(input=...) writes synchronously before timed waits.
    # An anonymous seekable file makes stdin immediately readable, with no pipe
    # writer that could block the deadline/heartbeat loop or outlive cancellation.
    with tempfile.TemporaryFile(mode='w+b') as request_file:
        request_file.write(request)
        request_file.seek(0)
        return _execute_route_worker(request_file, config, timeout, heartbeat)


def _execute_route_worker(request_file, config, timeout, heartbeat=None):
    started = time.monotonic()
    try:
        process = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                                    '--_route-worker'], stdin=request_file, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False)
    except OSError:
        return _classification_result(1, ['无法启动路线执行器。'], config, reason_code='internal_error')
    try:
        if heartbeat:
            heartbeat()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                process.kill()
                process.communicate()
                return _classification_result(1, ['单条路线超过运行时限。'], config, reason_code='resource_limit')
            try:
                output, _ = process.communicate(timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                if heartbeat:
                    heartbeat()
        if process.returncode != 0 or len(output) > 4 * 1024 * 1024:
            raise ExportError('worker失败。')
        result = _strict_json(output)
        if (not isinstance(result, dict) or result.get('kind') != 'route_shape_classification'
                or type(result.get('category')) is not int or result['category'] not in (1, 2, 3, 4)):
            raise ExportError('worker输出无效。')
        return result
    except KeyboardInterrupt:
        process.kill()
        process.communicate()
        raise
    except Exception:
        if process.poll() is None:
            process.kill()
        process.communicate()
        return _classification_result(1, ['路线执行器异常，未给出几何结论。'], config, reason_code='internal_error')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _display_identifier(value):
    text = value if isinstance(value, str) else '(无有效ID)'
    # Route IDs are untrusted terminal text. Escape control/directional codes.
    return json.dumps(text[:80], ensure_ascii=True)


def run_export_batch(index, selected, *, selection=None, config=None, timeout=60.,
                     input_stream=None, progress=None, worker=None, clock=None):
    """Interactive CLI orchestration; only explicit Yes authorizes a multi-run."""
    cfg = _shape_config(config)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 3600:
        raise ExportError('单路线timeout必须介于0.1和3600秒之间。')
    if not selected:
        raise ExportError('没有选中路线。')
    input_stream = sys.stdin if input_stream is None else input_stream
    progress = sys.stderr if progress is None else progress
    worker = _run_route_worker if worker is None else worker
    clock = time.monotonic if clock is None else clock
    started = clock()
    count, cache, measured, pilot_timeout = len(selected), {}, [], False
    pilot_elapsed = 0.
    tty = bool(getattr(progress, 'isatty', lambda: False)())
    progress.write(f'扫描完成：{index.format}，共{len(index.entries)}条，选中{count}条，'
                   f'共{sum(e["point_count"] for e in selected)}点，'
                   f'{sum(not e["schema_supported"] for e in selected)}条明显缺少可用单段坐标。\n')
    progress.flush()
    if count > 1:
        candidates = sorted((e for e in selected if e['schema_supported']),
                            key=lambda e: (e['point_count'], e['source_index']))
        sample_count = min(3, count - 1, len(candidates))
        slots = ([0] if sample_count == 1 else
                 [round(i * (len(candidates) - 1) / (sample_count - 1)) for i in range(sample_count)])
        samples = [candidates[slot] for slot in slots]
        progress.write(f'估时试算：最多{sample_count}条，每条不超过{min(5., timeout):g}秒；'
                       '尚未执行剩余整批，不生成报告。\n')
        progress.flush()
        for entry in samples:
            before = clock()
            result = worker(index.load(entry), cfg, min(5., timeout))
            duration = max(.001, clock() - before)
            pilot_elapsed += duration
            if result.get('reason_code') == 'resource_limit':
                pilot_timeout = True
            else:
                cache[entry['source_index']] = result
            # A completed geometric ambiguity with computed metrics is valid
            # timing evidence, unlike schema rejection, startup failure or timeout.
            if result['category'] != 1 or (result.get('reason_code') == 'ambiguous_shape'
                                            and result.get('metrics')):
                measured.append((entry['point_count'], duration))
            progress.write(f'试算 {entry["source_index"]} ID={_display_identifier(entry["topRouteId"])} '
                           f'完成，耗时{duration:.2f}秒。\n')
            progress.flush()
        remaining = [entry for entry in selected if entry['source_index'] not in cache]
        if measured and not pilot_timeout:
            predicted = 0.
            for entry in remaining:
                n, elapsed = min(measured, key=lambda pair: abs(pair[0] - entry['point_count']))
                predicted += elapsed * max(.5, min(4., entry['point_count'] / max(n, 1)))
            estimate = f'预计剩余约{predicted * .5:.1f}–{predicted * 2.5:.1f}秒（小样本估计，非保证）'
        else:
            estimate = '无法可靠估计：试算超时或没有有效分类耗时样本'
        progress.write(f'{estimate}；剩余{len(remaining)}条，逐条时限{timeout:g}秒，'
                       f'串行时限合计上界约{len(remaining) * timeout:.0f}秒（另有扫描/调度开销）。\n')
        while True:
            progress.write('继续处理并发布整批结果？[Yes/No，默认No]: ')
            progress.flush()
            answer = input_stream.readline().strip().lower()
            if answer in ('yes', 'y'):
                break
            if answer in ('', 'no', 'n'):
                progress.write('已取消；未执行剩余路线，未发布报告。\n')
                progress.flush()
                raise BatchCancelled()
            progress.write('请输入Yes或No。\n')
    index.verify()
    execution_started = clock()
    rows = []
    last_heartbeat = [execution_started]
    for entry in selected:
        done = len(rows)
        label = f'index={entry["source_index"]} ID={_display_identifier(entry["topRouteId"])}'
        def heartbeat():
            now = clock()
            elapsed = max(0., now - execution_started)
            if tty:
                progress.write(f'\r[{"#" * (20 * done // count):<20}] {done}/{count} '
                               f'{label} 运行中 已耗时{elapsed:.1f}s')
                progress.flush()
            elif now - last_heartbeat[0] >= 5.:
                progress.write(f'心跳 {label} 仍在运行，执行阶段已耗时{elapsed:.1f}s。\n')
                progress.flush()
                last_heartbeat[0] = now
        result = cache.get(entry['source_index'])
        if result is None:
            progress.write(f'开始 {label}\n')
            progress.flush()
            result = worker(index.load(entry), cfg, timeout, heartbeat=heartbeat)
        row = {key: entry[key] for key in ('source_index', 'source_line', 'topRouteId', 'warnings')}
        row['classification'] = result
        rows.append(row)
        elapsed = max(.001, clock() - execution_started + pilot_elapsed)
        remaining = (elapsed / len(rows)) * (count - len(rows))
        prefix = '\r' if tty else ''
        progress.write(f'{prefix}[{"#" * (20 * len(rows) // count):<20}] '
                       f'{len(rows)}/{count} {label} 类别{result["category"]} '
                       f'已耗时{elapsed:.1f}s 剩余约{remaining:.1f}s\n')
        progress.flush()
    index.verify(full_hash=True)
    categories = {str(n): sum(row['classification']['category'] == n for row in rows) for n in (1, 2, 3, 4)}
    return {'schema_version': '1.0', 'kind': 'route_shape_batch', 'status': 'complete',
            'input_sha256': index.source_sha256, 'input_format': index.format,
            'selection': selection or {}, 'source_records': len(index.entries),
            'selected_count': count, 'processed_count': len(rows),
            'elapsed_seconds': max(0., clock() - execution_started + pilot_elapsed),
            'wall_seconds_including_confirmation': max(0., clock() - started),
            'category_counts': categories,
            'execution_errors': sum(row['classification'].get('reason_code') == 'internal_error' for row in rows),
            'results': rows}


def compact_batch_results(batch):
    """Keep source order and duplicate IDs; do not turn rows into a lossy map."""
    return [{'topRouteId': row['topRouteId'], 'category': row['classification']['category']}
            for row in batch['results']]


def _preflight_output(path, source):
    if path is None:
        return
    target = Path(path)
    if target.exists() or target.is_symlink() or target.resolve() == Path(source).resolve():
        raise ExportError('输出已存在或指向输入，禁止覆盖。')
    if not target.parent.is_dir():
        raise ExportError('输出父目录不存在。')


def _publish_json(result, output=None):
    if output is None:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    # Write to an exclusively created sibling, then atomically create the final
    # pathname with a no-overwrite hard link. Never unlink the output pathname:
    # another process may have replaced it, even after our successful creation.
    target = Path(output)
    descriptor, temporary = tempfile.mkstemp(prefix='.route-report-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    if sys.argv[1:] == ['--_route-worker']:
        raise SystemExit(_worker_main())
    parser = argparse.ArgumentParser(description='Standalone GPX / exported JSON route shape classification.')
    parser.add_argument('input')
    parser.add_argument('--radius', type=float)
    parser.add_argument('--spacing', type=float)
    parser.add_argument('--out', help='NEW UTF-8 JSON report file; existing files are never overwritten.')
    parser.add_argument('--classify', action='store_true', help='Whole-route class 1/2/3/4; implicit for JSON.')
    parser.add_argument('--input-format', choices=('auto', 'gpx', 'json', 'ndjson'), default='auto')
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument('--top-route-id')
    selectors.add_argument('--start', type=int, help='1-based source record index; requires --count.')
    selectors.add_argument('--all', action='store_true', dest='all_routes')
    parser.add_argument('--count', type=int)
    parser.add_argument('--detailed', action='store_true', help='Include full diagnostics in JSON export results; default is ID/category pairs.')
    parser.add_argument('--route-timeout', type=float, default=60., help='Per-route worker wall time, including startup.')
    args = parser.parse_args()
    try:
        _preflight_output(args.out, args.input)
        input_format = args.input_format
        if input_format == 'auto':
            suffix = Path(args.input).suffix.lower()
            if suffix == '.gpx':
                input_format = 'gpx'
            elif suffix in ('.json', '.jsonl', '.ndjson'):
                input_format = 'auto'
            else:
                with Path(args.input).open('rb') as stream:
                    prefix = stream.read(4096).removeprefix(b'\xef\xbb\xbf').lstrip()
                input_format = 'gpx' if prefix.startswith(b'<') else 'auto'
        json_selection = args.top_route_id is not None or args.start is not None or args.count is not None or args.all_routes
        if input_format == 'gpx':
            if json_selection or args.route_timeout != 60. or args.detailed:
                raise ExportError('JSON选择器、detailed和route-timeout不能用于GPX。')
            if args.classify and (args.radius is not None or args.spacing is not None):
                raise ExportError('--radius / --spacing不能与--classify混用。')
            result = (classify_route(args.input) if args.classify else
                      recognize_loops(args.input, LoopRecognitionConfig(
                          match_radius_m=12. if args.radius is None else args.radius,
                          spacing_m=8. if args.spacing is None else args.spacing)))
            _publish_json(result, args.out)
            if args.classify and result['category'] == 1:
                parser.exit(2)
        else:
            if args.radius is not None or args.spacing is not None:
                raise ExportError('--radius / --spacing仅支持GPX详细诊断。')
            index = RouteExportIndex(args.input, input_format)
            selected = index.select(top_route_id=args.top_route_id, start=args.start,
                                    count=args.count, all_routes=args.all_routes)
            selection = ({'topRouteId': args.top_route_id} if args.top_route_id is not None else
                         {'all': True} if args.all_routes else {'start': args.start, 'count': args.count})
            result = run_export_batch(index, selected, selection=selection, timeout=args.route_timeout)
            _publish_json(result if args.detailed else compact_batch_results(result), args.out)
            if result['category_counts']['1']:
                parser.exit(2)
    except BatchCancelled:
        parser.exit(3)
    except KeyboardInterrupt:
        sys.stderr.write('\n已中断；未发布完整报告。\n')
        parser.exit(130)
    except (RecognitionError, ExportError, OSError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, (RecognitionError, ExportError)) else '输入或输出操作失败。'
        parser.exit(2, f'{message}\n')


if __name__ == '__main__':
    main()
