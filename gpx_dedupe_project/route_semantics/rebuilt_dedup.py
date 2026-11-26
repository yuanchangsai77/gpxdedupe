"""Conservative same-topology dedup of selected rebuilt routes, never deletes.

Candidate hash uses ordered directed edges and graph/activity/closure scope.
Only all-member geometric consistency permits joining an equivalence cluster.
"""

import argparse
import hashlib
import json
import os
import resource
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .city_store import CityStore, unpack
from .fast_geometry import corridor
from .geometry import distance, length

VERSION = "rebuilt-equivalence-1"
LIMITS = {
    "max_length_difference_m": 30,
    "max_relative_length_difference": 0.01,
    "max_endpoint_m": 15,
    "max_corridor_m": 8,
    "max_p95_m": 3,
}


def cyclic_minimum(sequence):
    """Booth rotation, retaining direction and number of visits."""
    if not sequence:
        return ()
    n = len(sequence)
    values = tuple(sequence) * 2
    i, j, k = 0, 1, 0
    while i < n and j < n and k < n:
        a, b = values[i + k], values[j + k]
        if a == b:
            k += 1
            continue
        if a > b:
            i = i + k + 1
            if i <= j:
                i = j + 1
        else:
            j = j + k + 1
            if j <= i:
                j = i + 1
        k = 0
    start = min(i, j)
    return values[start : start + n]


@dataclass
class Route:
    id: int
    points: list
    signature: str
    closed: bool
    length_m: float
    rank: tuple


def extract(row):
    geometry = unpack(row["rebuilt"]) or []
    definition = unpack(row["definition"]) or {}
    # Multipart structures are retained individually, not flattened across breaks.
    if len(geometry) != 1 or len(geometry[0]) < 2:
        return None, "multipart_or_empty"
    if definition.get("source_parts") != 1:
        return None, "multipart_source"
    points = geometry[0]
    closed = distance(points[0], points[-1]) <= 15
    edges = []
    windows = definition.get("definitions", [])
    if not windows:
        return None, "missing_windows"
    for window in windows:
        if window.get("source_part") != 0:
            return None, "unknown_source_part"
        current = window.get("road_evidence", {}).get("directed_edges", [])
        if not current or any(
            not isinstance(e, int) or isinstance(e, bool) for e in current
        ):
            return None, "missing_directed_edges"
        # A window boundary can split one continuous traversal of the same edge.
        for edge in current:
            if not edges or edges[-1] != edge:
                edges.append(edge)
    if closed:
        if len(edges) > 1 and edges[0] == edges[-1]:
            edges.pop()
        edges = cyclic_minimum(edges)
    scope = [
        row["graph_version"],
        row["algorithm_version"],
        row["activity_codes"],
        definition.get("activity_type"),
        closed,
        edges,
    ]
    signature = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    errors = [
        p.get("metrics", {}).get("maximum_corridor_error_m", float("inf"))
        for p in definition.get("products", [])
    ]
    return Route(
        row["id"],
        points,
        signature,
        closed,
        length(points),
        (max(errors, default=float("inf")), row["id"]),
    ), None


def compare(a, b):
    if a.signature != b.signature or a.closed != b.closed:
        return False, {"reason": "different_directed_topology"}
    delta = abs(a.length_m - b.length_m)
    relative = delta / max(a.length_m, b.length_m, 1)
    metrics = {"length_difference_m": delta, "relative_length_difference": relative}
    if (
        delta > LIMITS["max_length_difference_m"]
        or relative > LIMITS["max_relative_length_difference"]
    ):
        return False, {**metrics, "reason": "length_variant"}
    if not a.closed:
        endpoints = max(
            distance(a.points[0], b.points[0]), distance(a.points[-1], b.points[-1])
        )
        metrics["endpoint_m"] = endpoints
        if endpoints > LIMITS["max_endpoint_m"]:
            return False, {**metrics, "reason": "endpoint_variant"}
    if len(a.points) > 100000 or len(b.points) > 100000:
        return False, {**metrics, "reason": "geometry_budget"}
    forward = corridor(a.points, b.points)
    reverse = corridor(b.points, a.points)
    if forward is None or reverse is None:
        return False, {**metrics, "reason": "numeric_backend_missing"}
    maximum = max(max(forward), max(reverse))
    p95 = max(
        sorted(forward)[round(0.95 * (len(forward) - 1))],
        sorted(reverse)[round(0.95 * (len(reverse) - 1))],
    )
    metrics.update(maximum_corridor_m=maximum, corridor_p95_m=p95)
    passed = maximum <= LIMITS["max_corridor_m"] and p95 <= LIMITS["max_p95_m"]
    return passed, {
        **metrics,
        "reason": "same_directed_topology_and_geometry"
        if passed
        else "geometry_variant",
    }


def evidence_token(row):
    return json.dumps(
        [
            row["id"],
            row["graph_version"],
            row["algorithm_version"],
            row["activity_codes"],
            hashlib.sha256(row["definition"] or b"").hexdigest(),
            hashlib.sha256(row["rebuilt"] or b"").hexdigest(),
        ],
        sort_keys=True,
    ).encode()


def source_snapshot(store, ids):
    digest = hashlib.sha256()
    loaded = set()
    for row in store.db.execute(
        "SELECT id,definition,rebuilt,graph_version,algorithm_version,activity_codes FROM routes WHERE state='candidate' ORDER BY id"
    ):
        if row["id"] in ids:
            loaded.add(row["id"])
            digest.update(evidence_token(row))
    if loaded != ids:
        raise ValueError("dedup source selection changed")
    return digest.hexdigest()


def cluster(routes, comparison=compare, pair_log=None):
    """Complete-link within a hash bucket; never use transitive union-find."""
    groups = []
    comparisons = 0
    decisions = {}
    for route in sorted(routes, key=lambda r: r.rank):
        assigned = False
        for group in groups:
            evidence = []
            for member in group:
                equivalent, metrics = comparison(route, member)
                if pair_log is not None:
                    pair_log.append(
                        (
                            route.id,
                            member.id,
                            int(equivalent),
                            json.dumps(metrics, ensure_ascii=False),
                        )
                    )
                comparisons += 1
                if not equivalent:
                    break
                evidence.append((member.id, metrics))
            else:
                decisions[route.id] = evidence
                group.append(route)
                assigned = True
                break
        if not assigned:
            groups.append([route])
            decisions[route.id] = []
    return groups, decisions, comparisons


def run(source, coverage, output):
    if Path(output).resolve() in (Path(source).resolve(), Path(coverage).resolve()):
        raise ValueError("dedup output must be separate")
    began = time.monotonic()
    store = CityStore(source, initialize=False)
    selection = sqlite3.connect(f"file:{Path(coverage).resolve()}?mode=ro", uri=True)
    try:
        ids = {
            r[0]
            for r in selection.execute(
                "SELECT route_id FROM density_decisions WHERE selected=1 AND geometry_kind='rebuilt'"
            )
        }
        snapshot = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
        buckets = defaultdict(list)
        skipped = {}
        loaded = set()
        source_digest = hashlib.sha256()
        for row in store.db.execute(
            "SELECT id,definition,rebuilt,graph_version,algorithm_version,activity_codes FROM routes WHERE state='candidate' ORDER BY id"
        ):
            if row["id"] not in ids:
                continue
            loaded.add(row["id"])
            source_digest.update(evidence_token(row))
            route, reason = extract(row)
            if route is None:
                skipped[row["id"]] = reason
            else:
                buckets[route.signature].append(route)
        if loaded != ids:
            raise ValueError("selection contains missing/non-candidate routes")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(output)
        try:
            db.executescript("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,created REAL,report TEXT);
            CREATE TABLE IF NOT EXISTS members(run_id INTEGER,route_id INTEGER,canonical_id INTEGER,group_id TEXT,rank INTEGER,explanation TEXT,PRIMARY KEY(run_id,route_id));""")
            rows = []
            pair_log = []
            groups_count = comparisons = duplicates = 0
            for signature, bucket in buckets.items():
                groups, evidence, count = cluster(bucket, pair_log=pair_log)
                comparisons += count
                for group in groups:
                    canonical = group[0].id
                    groups_count += 1
                    duplicates += len(group) - 1
                    gid = hashlib.sha256(
                        f"{VERSION}:{signature}:{canonical}".encode()
                    ).hexdigest()[:24]
                    for rank, route in enumerate(group):
                        rows.append(
                            (
                                route.id,
                                canonical,
                                gid,
                                rank,
                                json.dumps(
                                    {
                                        "pair_checks": evidence[route.id],
                                        "basis": "ordered_directed_edges_plus_geometry",
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        )
            for rid, reason in skipped.items():
                rows.append(
                    (rid, rid, f"private:{rid}", 0, json.dumps({"reason": reason}))
                )
            report = {
                "version": VERSION,
                "input_routes": len(ids),
                "scope": "selected_rebuilt_only",
                "numeric_routes": sum(map(len, buckets.values())),
                "candidate_buckets": len(buckets),
                "pair_checks": comparisons,
                "duplicate_routes": duplicates,
                "representative_routes": len(ids) - duplicates,
                "duplicate_groups": None,
                "skipped": skipped,
                "limits": LIMITS,
                "snapshot": snapshot,
                "source_snapshot": source_digest.hexdigest(),
                "retained_candidate_pairs": sum(not row[2] for row in pair_log),
                "elapsed_s": time.monotonic() - began,
                "calibrated": False,
                "raw_routes_untouched": True,
            }
            # Count actual groups without rerunning geometric checks.
            counts = defaultdict(int)
            for _, canonical, _, _, _ in rows:
                counts[canonical] += 1
            report["duplicate_groups"] = sum(n > 1 for n in counts.values())
            db.execute(
                "CREATE TABLE IF NOT EXISTS pairs(run_id INTEGER,a INTEGER,b INTEGER,equivalent INTEGER,metrics TEXT,PRIMARY KEY(run_id,a,b))"
            )
            with db:
                cur = db.execute(
                    "INSERT INTO runs(created,report) VALUES(?,?)",
                    (time.time(), json.dumps(report)),
                )
                db.executemany(
                    "INSERT INTO members VALUES(?,?,?,?,?,?)",
                    [(cur.lastrowid, *row) for row in rows],
                )
                db.executemany(
                    "INSERT INTO pairs VALUES(?,?,?,?,?)",
                    [(cur.lastrowid, *pair) for pair in pair_log],
                )
            Path(output).with_suffix(".report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return report
        finally:
            db.close()
    finally:
        store.db.close()
        selection.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--coverage", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    run(a.source, a.coverage, a.output)
