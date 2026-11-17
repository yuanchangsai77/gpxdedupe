"""Read-only, geometry-only chronological GPX loop decomposition.

This is not a road-network reconstruction or an enumeration of all simple cycles.
Source intervals always refer to the unmodified input, including fractional edges.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial import cKDTree

try:
    from . import route_dedupe as rd
except ImportError:
    import route_dedupe as rd


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
        segs = root.findall('.//' + rd.tag('trkseg'))
        if len(segs) != 1:
            raise RecognitionError('环识别仅支持恰好一个 trkseg，不会跨断点连接。')
        points = segs[0].findall(rd.tag('trkpt'))
        if not 3 <= len(points) <= cfg.max_points:
            raise RecognitionError('轨迹点数超出支持范围。')
        coords = np.asarray([[float(p.attrib['lon']), float(p.attrib['lat'])] for p in points])
        if (not np.isfinite(coords).all() or np.any(np.abs(coords[:, 0]) > 180)
                or np.any(np.abs(coords[:, 1]) > 90)):
            raise RecognitionError('经纬度必须是有效的有限数值。')
        for point in points:
            elevation = point.findtext(rd.tag('ele'))
            if elevation is not None and not math.isfinite(float(elevation)):
                raise RecognitionError('海拔必须是有限数值。')
    except RecognitionError:
        raise
    except (ET.ParseError, ValueError, KeyError, TypeError, OverflowError, LookupError) as exc:
        raise RecognitionError('GPX 格式或坐标无效。') from exc
    if (np.ptp(coords[:, 0]) > 6 or np.ptp(coords[:, 1]) > 6
            or np.max(coords[:, 1]) > 84 or np.min(coords[:, 1]) < -80):
        raise RecognitionError('轨迹超出局部 UTM 支持范围；不支持跨日期变更线或大跨度轨迹。')
    xy, _, epsg = rd.projection(coords)
    distances = rd.arclength(xy)
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
    d = rd.arclength(closed)
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
            heading = rd.tangent(reference)
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
    d = rd.arclength(points)
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


def main():
    parser = argparse.ArgumentParser(description='Read-only GPX loop structure recognition (no cleaned track).')
    parser.add_argument('input')
    parser.add_argument('--radius', type=float, default=12.)
    parser.add_argument('--spacing', type=float, default=8.)
    parser.add_argument('--out', help='Optional NEW JSON report file; existing files are never overwritten.')
    args = parser.parse_args()
    try:
        result = recognize_loops(args.input, LoopRecognitionConfig(match_radius_m=args.radius, spacing_m=args.spacing))
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.out:
            with Path(args.out).open('x', encoding='utf-8') as output:
                output.write(text + '\n')
        else:
            print(text)
    except (RecognitionError, OSError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
