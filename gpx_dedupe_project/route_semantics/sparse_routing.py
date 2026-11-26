"""Bounded whole-route foot planning using persistent, sparse control points.

The resampled/filtered trace is selection evidence, never output geometry. Only
native engine geometry can become a road candidate. No GPS or snap connectors.
Thresholds are experimental and deliberately exposed in the audit, not calibrated.
"""

import math
from itertools import pairwise
from statistics import median
from urllib.error import HTTPError, URLError

from .fast_geometry import corridor as fast_corridor
from .fast_geometry import ordered as fast_ordered
from .geometry import distance, length, point_segment

VERSION = "sparse-road-5"


def resample(points, step=15, maximum=1500):
    result = [list(points[0])]
    travelled = 0
    target = step
    for a, b in pairwise(points):
        span = distance(a, b)
        while span and target <= travelled + span:
            if len(result) >= maximum:
                raise ValueError("sampling_resource_limit")
            ratio = (target - travelled) / span
            result.append([a[k] + (b[k] - a[k]) * ratio for k in (0, 1)])
            target += step
        travelled += span
    if result[-1] != list(points[-1]):
        result.append(list(points[-1]))
    return result


def stable_evidence(points):
    # Equal arclength is used only for noise filtering, NOT waypoint selection.
    samples = resample(points)
    if len(samples) > 1500:
        raise ValueError("evidence_resource_limit")
    filtered = [samples[0]]
    for i in range(1, len(samples) - 1):
        window = samples[max(0, i - 2) : min(len(samples), i + 3)]
        filtered.append([median(p[k] for p in window) for k in (0, 1)])
    filtered.append(samples[-1])
    return filtered


def simplify(points, tolerance):
    selected = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        a, b = pending.pop()
        values = (
            fast_corridor(points[a + 1 : b], [points[a], points[b]])
            if b - a > 1
            else []
        )
        choices = (
            list(zip(values, range(a + 1, b)))
            if values is not None
            else [
                (point_segment(points[i], points[a], points[b]), i)
                for i in range(a + 1, b)
            ]
        )
        if choices:
            error, i = max(choices)
            if error > tolerance:
                selected.add(i)
                pending.extend([(a, i), (i, b)])
    return sorted(selected)


def ordered_simplify(points, tolerance=60):
    selected = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        a, b = pending.pop()
        if b - a < 2:
            continue
        values = fast_corridor(points[a + 1 : b], [points[a], points[b]])
        error, i = (
            max(zip(values, range(a + 1, b)))
            if values is not None
            else max(
                (point_segment(points[j], points[a], points[b]), j)
                for j in range(a + 1, b)
            )
        )
        if error <= tolerance:
            dx = (points[b][0] - points[a][0]) * math.cos(math.radians(points[a][1]))
            dy = points[b][1] - points[a][1]
            norm = dx * dx + dy * dy
            if not norm:
                continue
            scale = distance(points[a], points[b])
            peak = 0
            peak_index = a
            worst = (0, a)
            for j in range(a + 1, b + 1):
                t = (
                    (points[j][0] - points[a][0])
                    * math.cos(math.radians(points[a][1]))
                    * dx
                    + (points[j][1] - points[a][1]) * dy
                ) / norm
                if (peak - t) * scale > worst[0]:
                    worst = ((peak - t) * scale, peak_index)
                if t > peak:
                    peak, peak_index = t, j
            if worst[0] <= 80:
                continue
            i = worst[1]
        if not a < i < b:
            continue
        selected.add(i)
        pending.extend([(a, i), (i, b)])
    return sorted(selected)


def select_controls(points, maximum=12):
    evidence = stable_evidence(points)
    tolerance = 60.0
    indices = simplify(evidence, tolerance)
    while len(indices) > maximum:
        tolerance *= 1.3
        indices = simplify(evidence, tolerance)
    # Geometric simplification alone loses collinear repeated out-and-backs.
    # Pin sustained heading reversals (about 60m of evidence on either side),
    # selecting one robust center per run rather than every turning sample.
    runs, run = [], []
    for i in range(4, len(evidence) - 4):
        a, p, b = evidence[i - 4], evidence[i], evidence[i + 4]
        u, v = [p[k] - a[k] for k in (0, 1)], [b[k] - p[k] for k in (0, 1)]
        norm = sum(x * x for x in u) ** 0.5 * sum(x * x for x in v) ** 0.5
        cosine = sum(x * y for x, y in zip(u, v)) / norm if norm else 1
        if cosine < -0.7 and min(distance(a, p), distance(p, b)) >= 35:
            run.append((cosine, i))
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    pins = [min(run)[1] for run in runs]
    indices = sorted(set(indices + pins))
    # Never use a raw extreme: each interior coordinate is a local robust center.
    return evidence, indices, tolerance


def errors(points, route):
    accelerated = fast_corridor(points, route)
    if accelerated is not None:
        return accelerated
    return [min(point_segment(p, a, b) for a, b in pairwise(route)) for p in points]


def ordered_residuals(evidence, candidate):
    if len(evidence) * len(candidate) > 1500000:
        raise ValueError("ordered_validation_resource_limit")
    accelerated = fast_ordered(evidence, candidate)
    if accelerated is not None:
        return accelerated
    costs, parents = [], []
    for i, a in enumerate(evidence):
        row, parent = [], []
        for j, b in enumerate(candidate):
            if i == j == 0:
                best, direction = 0, 0
            else:
                choices = []
                if i and j:
                    choices.append((costs[i - 1][j - 1], 0))
                if i:
                    choices.append((costs[i - 1][j], 1))
                if j:
                    choices.append((row[j - 1], 2))
                best, direction = min(choices)
            row.append(max(best, distance(a, b)))
            parent.append(direction)
        costs.append(row)
        parents.append(parent)
    residuals = [0] * len(evidence)
    i, j = len(evidence) - 1, len(candidate) - 1
    while True:
        residuals[i] = max(residuals[i], distance(evidence[i], candidate[j]))
        if i == j == 0:
            break
        direction = parents[i][j]
        if direction != 2:
            i -= 1
        if direction != 1:
            j -= 1
    return costs[-1][-1], residuals


def quality(evidence, geometry, incumbent=None):
    if len(geometry) < 2:
        raise ValueError("empty_engine_geometry")
    # Oversized rejected detours must produce a diagnosis, not masquerade as a
    # failure to find a road. Accepted routes retain the original 20 m sampling.
    candidate_length = length(geometry)
    observed_length = max(length(evidence), 1)
    rejection_sampling = (
        candidate_length > 28000 and candidate_length / observed_length > 1.35
    )
    rejected_point_budget = min(1400, 1496000 // max(len(evidence), 1) - 2)
    candidate_step = (
        max(20, candidate_length / rejected_point_budget) if rejection_sampling else 20
    )
    candidate = resample(geometry, candidate_step)
    if len(candidate) > 1500:
        raise ValueError("candidate_resource_limit")
    forward, reverse = errors(evidence, candidate), errors(candidate, evidence)

    def percentile(values):
        return sorted(values)[int((len(values) - 1) * 0.95)]

    ratio = candidate_length / observed_length
    metrics = {
        "candidate_sampling_step_m": candidate_step,
        "coarse_sampling_for_rejection_only": rejection_sampling,
        "forward_p95_m": percentile(forward),
        "reverse_p95_m": percentile(reverse),
        "maximum_corridor_error_m": max(forward + reverse),
        "ordered_error_m": None,
        "distance_ratio": ratio,
        "endpoint_snap_m": max(
            distance(evidence[0], geometry[0]), distance(evidence[-1], geometry[-1])
        ),
    }
    cheap_pass = (
        max(metrics["forward_p95_m"], metrics["reverse_p95_m"]) <= 45
        and metrics["maximum_corridor_error_m"] <= 85
        and 0.7 <= ratio <= 1.35
        and metrics["endpoint_snap_m"] <= 45
    )
    lower_bound = max(
        metrics["forward_p95_m"] / 45,
        metrics["reverse_p95_m"] / 45,
        metrics["maximum_corridor_error_m"] / 85,
        metrics["endpoint_snap_m"] / 45,
        ratio / 1.35,
        0.7 / max(ratio, 0.001),
    )
    # Exact branch-and-bound: a candidate that fails cheap gates AND cannot
    # improve the incumbent cannot become accepted or selected after Frechet.
    if incumbent is not None and not cheap_pass and lower_bound >= incumbent - 0.03:
        return False, metrics, None
    order, residuals = ordered_residuals(evidence, candidate)
    metrics["ordered_error_m"] = order
    # A local noisy observation cannot force a waypoint; sustained/order failures
    # still prevent shortcuts, lost loops or unbounded crossing detours.
    passed = (
        max(metrics["forward_p95_m"], metrics["reverse_p95_m"]) <= 45
        and metrics["maximum_corridor_error_m"] <= 85
        and order <= 70
        and 0.70 <= ratio <= 1.35
        and metrics["endpoint_snap_m"] <= 45
    )
    return passed, metrics, [max(a, b) for a, b in zip(forward, residuals)]


def refinement(evidence, indices, forward, minimum_separation=100):
    cumulative = [0]
    for a, b in pairwise(evidence):
        cumulative.append(cumulative[-1] + distance(a, b))
    # Require a sustained disagreement, not the maximum single drifting point.
    runs, run = [], []
    for i, error in enumerate(forward):
        if error > 45:
            run.append(i)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    for run in sorted(
        runs, key=lambda r: cumulative[r[-1]] - cumulative[r[0]], reverse=True
    ):
        if cumulative[run[-1]] - cumulative[run[0]] < 45:
            continue
        choices = [
            i
            for i in run
            if all(
                abs(cumulative[i] - cumulative[j]) >= minimum_separation
                for j in indices
            )
        ]
        if not choices:
            continue
        # Middle of supported disagreement, away from raw extremes / divergence
        # boundaries. Insertion is by temporal index, preserving repeated visits.
        i = choices[len(choices) // 2]
        return i
    return None


def build(row, adapter, maximum_controls=18, maximum_requests=24):
    if maximum_controls < 2 or maximum_requests < 1:
        raise ValueError("invalid_planning_limits")
    points = row["original"]
    evidence, indices, tolerance = select_controls(points)
    attempts = []
    result = {
        "version": VERSION,
        "mode": "road_unavailable",
        "segments": [],
        "attempts": attempts,
        "road_observation_coverage": 0,
        "source_observation_coverage": 0,
        "navigation_enabled": False,
        "start": points[0],
        "finish": points[-1],
        "selection_tolerance_m": tolerance,
        "initial_control_count": len(indices),
        "road_graph_version": getattr(adapter, "graph_version", None),
        "quality_limits": {
            "corridor_p95_m": 45,
            "maximum_corridor_m": 85,
            "ordered_m": 70,
            "endpoint_snap_m": 45,
            "minimum_distance_ratio": 0.70,
            "maximum_distance_ratio": 1.35,
        },
        "candidate": [],
        "quality_summary": "仅输出道路规划；无可靠结果时不使用原轨迹衔接。",
        "selection_policy": "15m evidence sampling, 5-sample median, 60m adaptive shape, 100m refinement separation",
    }

    def controls():
        return [
            {
                "coordinate": evidence[i],
                "evidence_index": i,
                "kind": "endpoint"
                if i in (0, len(evidence) - 1)
                else "stable_shape_control",
            }
            for i in indices
        ]

    if row["id"] == "chengdu-07":
        result.update(
            mode="facility_unavailable",
            controls=controls(),
            quality_summary="操场尚无已验证的可路由跑道；仅保留原始轨迹对照，不冒充重建。",
        )
        return result

    if len(indices) > maximum_controls:
        result.update(
            controls=controls(), failure_reason="structural_controls_exceed_budget"
        )
        return result

    def score(metrics):
        return max(
            metrics["forward_p95_m"] / 45,
            metrics["reverse_p95_m"] / 45,
            metrics["maximum_corridor_error_m"] / 85,
            metrics["ordered_error_m"] / 70,
            metrics["endpoint_snap_m"] / 45,
            metrics["distance_ratio"] / 1.35,
            0.70 / max(metrics["distance_ratio"], 0.001),
        )

    def evaluate(selected, operation, incumbent=None):
        attempt = {
            "iteration": len(attempts),
            "control_count": len(selected),
            "evidence_indices": list(selected),
            "operation": operation,
        }
        attempts.append(attempt)
        try:
            route = adapter.route([evidence[i] for i in selected], "foot")
            geometry = route["geometry"]["coordinates"]
            passed, metrics, forward = quality(evidence, geometry, incumbent)
            if forward is None:
                attempt.update(metrics, status="pruned_by_quality_lower_bound")
                return None
            attempt.update(
                metrics,
                status="accepted_candidate" if passed else "rejected",
                score=score(metrics),
            )
            return (
                score(metrics),
                passed,
                geometry,
                metrics,
                forward,
                list(selected),
                {
                    "planning_controls": route.get("planning_controls"),
                    "endpoint_adjustments": route.get("endpoint_adjustments", []),
                    "osm_way_ids": route.get("osm_way_ids", []),
                    "directed_edges": route.get("directed_edges", []),
                },
            )
        except (ValueError, HTTPError, URLError, TimeoutError) as exc:
            attempt.update(status="failed", reason=str(exc)[:240])
            if isinstance(exc, HTTPError):
                attempt["http_status"] = exc.code
                attempt["engine_error"] = exc.read(2048).decode(
                    "utf-8", errors="replace"
                )
            return None

    best = evaluate(indices, "initial_sparse_controls")
    # The same road can be traversed multiple times. A purely spatial RDP can
    # erase collinear reversals inside a long chord. Retry with order-aware
    # structural points, retaining the same engine, quality gates and request cap.
    ordered_indices = ordered_simplify(evidence)
    if (
        (not best or not best[1])
        and ordered_indices != indices
        and len(ordered_indices) <= 32
    ) and len(attempts) < maximum_requests:
        trial = evaluate(
            ordered_indices,
            "restore_ordered_repeated_visits",
            best[0] if best else None,
        )
        if trial and (
            best is None or (trial[1] and not best[1]) or trial[0] < best[0] - 0.03
        ):
            best = trial
            indices = best[5]
            maximum_controls = max(maximum_controls, len(indices))
            result["selection_strategy"] = "order_aware_structural_retry"

    # Test removal BEFORE adding points. Wrongly snapped interior controls often
    # cause exactly the unsupported return that more GPS constraints exacerbate.
    # Retain a removal only if whole-route semantic/order quality improves.
    for _ in range(2):
        if best and best[1]:
            break
        previous = best
        for i in indices[1:-1]:
            if len(attempts) >= maximum_requests - 4:
                break
            trial = evaluate(
                [j for j in indices if j != i],
                "test_remove_control",
                best[0] if best else None,
            )
            if trial and (
                best is None or (trial[1] and not best[1]) or trial[0] < best[0] - 0.03
            ):
                best = trial
            if best and best[1]:
                break
        if best is previous:
            break
        indices = best[5]
    tested = set()
    while (
        best
        and not best[1]
        and len(attempts) < maximum_requests
        and len(indices) < maximum_controls
    ):
        new = refinement(evidence, sorted(set(indices) | tested), best[4])
        if new is None:
            break
        tested.add(new)
        trial = evaluate(
            sorted([*indices, new]), "test_add_supported_branch_control", best[0]
        )
        if trial and ((trial[1] and not best[1]) or trial[0] < best[0] - 0.03):
            best = trial
            indices = best[5]
    if best:
        _, passed, geometry, metrics, _, indices, road_evidence = best
        result["road_evidence"] = road_evidence
        result["candidate"] = geometry
        result["metrics"] = metrics
        if passed:
            result.update(
                mode="road_candidate",
                road_observation_coverage=1,
                source_observation_coverage=1,
                segments=[
                    {
                        "kind": "road",
                        "geometry": {"type": "LineString", "coordinates": geometry},
                        "source_span": [0, len(points) - 1],
                        "navigation_verified": False,
                        "graph_version": adapter.graph_version,
                        "reason": "whole_foot_route_sparse_stable_controls",
                    }
                ],
                quality_summary="稀疏关键点全程步行规划，已通过本轮实验走廊与行程顺序检查，仍需审核。",
            )
    if result["mode"] != "road_candidate" and attempts:
        result["failure_reason"] = (
            "planning_budget_reached"
            if len(attempts) >= maximum_requests
            else "no_quality_improving_stable_control"
        )
    result["controls"] = controls()
    if best and result.get("road_evidence", {}).get("planning_controls"):
        planned = result["road_evidence"]["planning_controls"]
        for control, coordinate in zip(result["controls"], planned):
            if control["coordinate"] != coordinate:
                control["observed_coordinate"] = control["coordinate"]
                control["coordinate"] = coordinate
                control["coordinate_origin"] = "endpoint_recovery_observed_sample"
        result["endpoint_adjustments"] = result["road_evidence"]["endpoint_adjustments"]
        omitted = sum(
            adjustment[side]["omitted_observed_m"]
            for adjustment in result["endpoint_adjustments"]
            for side in ("planning_start", "planning_end")
        )
        result["omitted_observed_distance_m"] = omitted
        if result["mode"] == "road_candidate" and omitted:
            result["road_observation_coverage"] = max(
                0, 1 - omitted / max(length(points), 1)
            )
            result["source_observation_coverage"] = result["road_observation_coverage"]

    return result


def validate_coverage(product, source):
    # Unlike the retired mixed display, snapped engine endpoints stay on roads.
    # They are never joined to observations by synthetic straight segments.
    if any(s["kind"] != "road" for s in product["segments"]):
        raise ValueError("non_road_fallback_forbidden")
    if product["mode"] == "road_candidate":
        if len(product["segments"]) != 1:
            raise ValueError("whole_route_required")
        coords = product["segments"][0]["geometry"]["coordinates"]
        if max(distance(coords[0], source[0]), distance(coords[-1], source[-1])) > 45:
            raise ValueError("endpoint_snap_limit")
    elif product["segments"]:
        raise ValueError("unaccepted_route_geometry")
    return product
