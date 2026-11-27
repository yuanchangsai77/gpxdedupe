"""Exact collinear intervals from complete reconstructed geometry, not GPS grids.

Native coordinates are quantized at 1e-6 degrees. Only exactly coincident lines
match; nearby/parallel roads do not. Split endpoints create shared atomic spans.
"""

import argparse
import bisect
import hashlib
import itertools
import json
import math
import os
import resource
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from .city_store import CityStore, unpack
from .geometry import distance, length
from .rebuilt_dedup import evidence_token

VERSION = "road-overlap-index-2"


def make_index(routes):
    lines = {}
    descriptions = {}
    raw = {}
    for route in routes:
        rid = route["id"]
        parts = route["parts"]
        raw[rid] = route
        if len(parts) != 1 or len(parts[0]) < 2:
            descriptions[rid] = {"skip": "multipart_or_empty", "segments": []}
            continue
        points = parts[0]
        segments = []
        for a, b in itertools.pairwise(points):
            x, y = round(a[0] * 1e6), round(a[1] * 1e6)
            u, v = round(b[0] * 1e6), round(b[1] * 1e6)
            dx, dy = u - x, v - y
            g = math.gcd(abs(dx), abs(dy))
            if not g:
                continue
            dx, dy = dx // g, dy // g
            if dx < 0 or (dx == 0 and dy < 0):
                dx, dy = -dx, -dy
            key = (route["scope"], dx, dy, -dy * x + dx * y)
            t0, t1 = x * dx + y * dy, u * dx + v * dy
            lo, hi = sorted((t0, t1))
            line = lines.setdefault(
                key, {"events": defaultdict(int), "base": (x, y, t0)}
            )
            line["events"][lo] += 1
            line["events"][hi] -= 1
            segments.append((key, lo, hi, t1 < t0))
        descriptions[rid] = {
            "segments": segments,
            "gap": distance(points[0], points[-1]),
            "traveled_m": length(points),
        }
    weights = []
    nodes = []
    for key, line in lines.items():
        _, dx, dy, _ = key
        x, y, origin = line["base"]
        norm = dx * dx + dy * dy
        bounds = sorted(line["events"])
        ids = []
        active = 0

        def coordinate(t, x=x, y=y, origin=origin, dx=dx, dy=dy, norm=norm):
            return (
                (x + (t - origin) * dx / norm) / 1e6,
                (y + (t - origin) * dy / norm) / 1e6,
            )

        for lo, hi in itertools.pairwise(bounds):
            active += line["events"][lo]
            if active:
                a, b = coordinate(lo), coordinate(hi)
                ids.append(len(weights))
                weights.append(distance(a, b))
                nodes.append(
                    (tuple(round(k * 1e6) for k in a), tuple(round(k * 1e6) for k in b))
                )
            else:
                ids.append(-1)
        line["bounds"] = bounds
        line["ids"] = ids
    output = {}
    for rid, description in descriptions.items():
        if description.get("skip"):
            output[str(rid)] = {
                "id": rid,
                "atoms": [],
                "walk": [],
                "protect": description["skip"],
                "physical_m": 0,
            }
            continue
        walk = []
        for key, lo, hi, reverse in description["segments"]:
            line = lines[key]
            a = bisect.bisect_left(line["bounds"], lo)
            b = bisect.bisect_left(line["bounds"], hi)
            ids = line["ids"][a:b]
            if reverse:
                ids = list(reversed(ids))
            walk.extend(i for i in ids if i >= 0)
        atoms = sorted(set(walk))
        physical = sum(weights[i] for i in atoms)
        adjacency = defaultdict(set)
        for atom in atoms:
            for node in nodes[atom]:
                adjacency[node].add(atom)
        pending = deque(node for node, edges in adjacency.items() if len(edges) < 2)
        removed = set()
        while pending:
            node = pending.popleft()
            for atom in list(adjacency[node]):
                if atom in removed:
                    continue
                removed.add(atom)
                for endpoint in nodes[atom]:
                    adjacency[endpoint].discard(atom)
                    if len(adjacency[endpoint]) == 1:
                        pending.append(endpoint)
        core = sum(weights[i] for i in atoms if i not in removed)
        gap = description["gap"]
        traveled = description["traveled_m"]
        near_ring = (
            gap <= 30
            and traveled >= max(100, 10 * gap)
            and physical / max(traveled, 1) >= 0.8
        )
        genuine_ring = gap <= 30 and core >= min(200, physical * 0.5) and core > 0
        protect = "closed_or_near_closed_loop" if genuine_ring or near_ring else None
        output[str(rid)] = {
            "id": rid,
            "atoms": atoms,
            "walk": walk,
            "physical_m": physical,
            "traveled_m": traveled,
            "endpoint_gap_m": gap,
            "cycle_core_m": core,
            "protect": protect,
            "quality": raw[rid].get("quality", 0),
        }
    return {
        "version": VERSION,
        "precision_degrees": 1e-6,
        "matching": "exact_collinear_intervals_undirected",
        "weights": weights,
        "routes": output,
    }


def run(source, output, coverage=None):
    inputs = {Path(source).resolve()}
    if coverage:
        inputs.add(Path(coverage).resolve())
    target = Path(output)
    ids_target = target.with_name("overlap-selected-ids.json")
    if target.resolve() in inputs or (coverage and ids_target.resolve() in inputs):
        raise ValueError("overlap outputs must differ from input databases")
    selected_ids = None
    if coverage:
        with sqlite3.connect(
            f"file:{Path(coverage).resolve()}?mode=ro", uri=True
        ) as db:
            selected_ids = [
                r[0]
                for r in db.execute(
                    "SELECT route_id FROM density_decisions WHERE selected=1 AND geometry_kind='rebuilt' ORDER BY route_id"
                )
            ]
    started = time.monotonic()
    store = CityStore(source, initialize=False)
    routes = []
    snapshot = hashlib.sha256()
    try:
        for row in store.db.execute(
            "SELECT id,definition,rebuilt,graph_version,algorithm_version,activity_codes FROM routes WHERE state='candidate' ORDER BY id"
        ):
            definition = unpack(row["definition"]) or {}
            snapshot.update(evidence_token(row))
            scope = json.dumps(
                [row["graph_version"], definition.get("activity_type")], sort_keys=True
            )
            metrics = [
                p.get("metrics", {}).get("maximum_corridor_error_m", 1000)
                for p in definition.get("products", [])
            ]
            routes.append(
                {
                    "id": row["id"],
                    "parts": unpack(row["rebuilt"]) or [],
                    "scope": scope,
                    "quality": max(metrics, default=1000),
                }
            )
        result = make_index(routes)
        result["source_snapshot"] = snapshot.hexdigest()
        result["route_ids"] = [r["id"] for r in routes]
        result["elapsed_s"] = time.monotonic() - started
        if selected_ids is not None and not set(selected_ids).issubset(
            result["route_ids"]
        ):
            raise ValueError("coverage selection does not match candidate source")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".next.json")
        temporary.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False))
        temporary.replace(target)
        if selected_ids is not None:
            ids_target.write_text(json.dumps(selected_ids))
        summary = {
            "routes": len(routes),
            "atoms": len(result["weights"]),
            "loop_protected": sum(
                r["protect"] == "closed_or_near_closed_loop"
                for r in result["routes"].values()
            ),
            "elapsed_s": result["elapsed_s"],
        }
        print(json.dumps(summary), flush=True)
        return result
    finally:
        store.db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--coverage")
    a = p.parse_args()
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    run(a.source, a.output, a.coverage)
