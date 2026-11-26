"""Resumable city-scale ingestion and bounded parallel reconstruction."""

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
import threading
import time
from itertools import pairwise
from pathlib import Path
from urllib.error import HTTPError, URLError

from .adapters import GraphHopperAdapter
from .city_store import CityStore, pack, unpack
from .failure_diagnosis import diagnose
from .geometry import distance, length
from .sparse_routing import VERSION, build

ALGORITHM = "city-1/" + VERSION
MAX_RECORD = 32 * 1024 * 1024


def display_geometry(parts, spacing=80):
    result = []
    for points in parts:
        if not points:
            continue
        selected = [points[0]]
        for p in points[1:-1]:
            if distance(selected[-1], p) >= spacing:
                selected.append(p)
        if selected[-1] != points[-1]:
            selected.append(points[-1])
        result.append(selected)
    return result


def ingest(source, database):
    store = CityStore(database)
    path = Path(source).resolve()
    stat = path.stat()
    namespace = hashlib.sha256(
        json.dumps([str(path), stat.st_size, stat.st_mtime_ns]).encode()
    ).hexdigest()
    prior = store.db.execute(
        "SELECT * FROM sources WHERE id=?", (namespace,)
    ).fetchone()
    if prior and prior["complete"]:
        return store.summary()
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO sources(id,path,size,mtime_ns) VALUES(?,?,?,?)",
            (namespace, str(path), stat.st_size, stat.st_mtime_ns),
        )
    count = 0
    began = time.perf_counter()
    with path.open("rb") as stream:
        stream.seek(prior["offset"] if prior else 0)
        while True:
            offset = stream.tell()
            raw = stream.readline(MAX_RECORD + 1)
            if not raw:
                break
            if len(raw) > MAX_RECORD:
                raise ValueError("oversize_record_checkpoint_not_advanced")
            end = stream.tell()
            parts = []
            error = None
            codes = []
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise TypeError("record_must_be_object")
                codes = record.get("activities", [])
                segments = record.get("segments", [])
                if not isinstance(segments, list):
                    raise TypeError("segments_must_be_array")
                for segment in segments:
                    if not isinstance(segment, dict):
                        raise TypeError("segment_must_be_object")
                    observations = segment.get("routePoints", [])
                    if not isinstance(observations, list) or any(
                        not isinstance(point, dict) for point in observations
                    ):
                        raise TypeError("route_points_must_be_objects")
                    points = [[p["longitude"], p["latitude"]] for p in observations]
                    if len(points) < 2:
                        continue
                    if any(
                        not all(type(v) in (float, int) and math.isfinite(v) for v in p)
                        or not -180 <= p[0] <= 180
                        or not -85 <= p[1] <= 85
                        for p in points
                    ):
                        raise ValueError("invalid_coordinate")
                    parts.append(points)
                if not parts:
                    raise ValueError("no_usable_segments")
                if sum(map(len, parts)) > 250000:
                    raise ValueError("point_resource_limit")
            except (ValueError, KeyError, TypeError) as exc:
                error = str(exc)[:200]
                parts = []
            flat = [p for points in parts for p in points]
            bounds = (
                (
                    min(p[0] for p in flat),
                    max(p[0] for p in flat),
                    min(p[1] for p in flat),
                    max(p[1] for p in flat),
                )
                if flat
                else (None,) * 4
            )
            store.db.execute(
                "INSERT OR IGNORE INTO routes(source_id,source_offset,content_sha256,activity_codes,parts,display,point_count,distance_m,min_lon,max_lon,min_lat,max_lat,state,error,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    namespace,
                    offset,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps(codes),
                    pack(parts),
                    pack(display_geometry(parts)),
                    len(flat),
                    sum(length(p) for p in parts),
                    *bounds,
                    "invalid" if error else "queued",
                    error,
                    time.time(),
                ),
            )
            row = store.db.execute(
                "SELECT id FROM routes WHERE source_id=? AND source_offset=?",
                (namespace, offset),
            ).fetchone()
            if flat:
                store.db.execute(
                    "INSERT OR REPLACE INTO route_bounds VALUES(?,?,?,?,?)",
                    (row[0], *bounds),
                )
            store.db.execute("UPDATE sources SET offset=? WHERE id=?", (end, namespace))
            count += 1
            if count % 100 == 0:
                store.db.commit()
                if count % 1000 == 0:
                    print(
                        "INGEST",
                        count,
                        "offset",
                        end,
                        "seconds",
                        round(time.perf_counter() - began, 1),
                        flush=True,
                    )
        store.db.execute("UPDATE sources SET complete=1 WHERE id=?", (namespace,))
        store.db.commit()
    store.meta(
        "ingestion",
        {
            "rows": count,
            "elapsed_s": time.perf_counter() - began,
            "source_id": namespace,
            "source_immutable": True,
        },
    )
    print(
        "INGEST_COMPLETE", json.dumps(store.summary(), ensure_ascii=False), flush=True
    )
    return store.summary()


def windows(points, maximum_m=12000):
    start = 0
    travelled = 0
    for i in range(1, len(points)):
        travelled += distance(points[i - 1], points[i])
        if travelled >= maximum_m and i - start >= 3:
            yield points[start : i + 1]
            start = i
            travelled = 0
    if start < len(points) - 1:
        yield points[start:]


_local = threading.local()


def process(row, url, graph):
    began = time.perf_counter()
    if not getattr(_local, "adapter", None):
        _local.adapter = GraphHopperAdapter(url, graph)
    engine = _local.adapter
    count = engine.route_request_count
    parts = unpack(row["parts"])
    products = []
    output = []
    definitions = []
    review = False
    failed = False

    class Budget:
        graph_version = graph

        def route(self, controls, mode):
            if (
                engine.route_request_count - count >= 64
                or time.perf_counter() - began > 90
            ):
                raise ValueError("whole_route_resource_budget")
            return engine.route(controls, mode)

    try:
        for original_index, original in enumerate(parts):
            joined = []
            last = None
            for segment in windows(original):
                product = build(
                    {
                        "id": "chengdu-07"
                        if row.get("facility_mode")
                        else "city:" + str(row["id"]),
                        "original": segment,
                    },
                    Budget(),
                )
                products.append(product)
                diagnosis = diagnose(product)
                product["diagnosis"] = diagnosis
                geometry = (
                    product["segments"][0]["geometry"]["coordinates"]
                    if product["mode"] == "road_candidate"
                    else product.get("candidate", [])
                    if diagnosis["review_preview"]
                    else []
                )
                if not geometry:
                    failed = True
                elif last and distance(last, geometry[0]) > 0.05:
                    failed = True
                    product["connection_error"] = "native_segment_snap_discontinuity"
                if geometry:
                    joined.extend(geometry if not joined else geometry[1:])
                    last = geometry[-1]
                if diagnosis["review_preview"]:
                    review = True
                controls = product.get("controls", [])
                definitions.append(
                    {
                        "source_part": original_index,
                        "start_anchor": controls[0] if controls else None,
                        "ordered_segments": [
                            {
                                "from": a,
                                "to": b,
                                "mode": "foot",
                                "mandatory_edges": [],
                                "forbidden_edges": [],
                            }
                            for a, b in pairwise(controls)
                        ],
                        "end_anchor": controls[-1] if controls else None,
                        "road_evidence": product.get("road_evidence", {}),
                        "quality": product.get("metrics"),
                        "status": product["mode"],
                    }
                )
            if joined:
                output.append(joined)
        state = "failed" if failed else "review" if review else "candidate"
        audit_attempts = [a for p in products for a in p.get("attempts", [])]
        if any(
            a.get("http_status") in (429, 500, 502, 503, 504)
            or "Connection refused" in a.get("reason", "")
            or "timed out" in a.get("reason", "")
            for a in audit_attempts
        ):
            state = "retryable"
        elif any(
            "whole_route_resource_budget" in a.get("reason", "") for a in audit_attempts
        ):
            state = "resource_limited"
        observed_cells = sorted(
            {
                (int(p[0] * 100), int(p[1] * 100))
                for part in unpack(row["display"])
                for p in part
            }
        )

        road_ids = sorted(
            {
                way
                for p in products
                for way in p.get("road_evidence", {}).get("osm_way_ids", [])
            }
        )
        payload = {
            "schema_version": "city-route-1",
            "source_sha256": row["content_sha256"],
            "algorithm_version": ALGORITHM,
            "road_graph_version": graph,
            "activity_type": "foot",
            "activity_assumed": True,
            "source_activity_codes": json.loads(row["activity_codes"]),
            "definitions": definitions,
            "products": products,
            "source_parts": len(parts),
            "elapsed_s": time.perf_counter() - began,
            "routing_requests": engine.route_request_count - count,
            "navigation_enabled": False,
            "dedup_executed": False,
            "geographical_fingerprint": hashlib.sha256(
                json.dumps(observed_cells).encode()
            ).hexdigest(),
            "fingerprint_basis": "validated_road_ids"
            if state == "candidate"
            else "road_semantics_unconfirmed",
            "core_fingerprint": hashlib.sha256(
                json.dumps(road_ids).encode()
            ).hexdigest()
            if road_ids and state == "candidate"
            else None,
            "version_fingerprint": hashlib.sha256(
                json.dumps(definitions, sort_keys=True).encode()
            ).hexdigest(),
        }
        return (
            row["id"],
            state,
            payload,
            output if state in ("candidate", "review") else [],
            time.perf_counter() - began,
        )
    except (
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        MemoryError,
        RuntimeError,
        OSError,
    ) as exc:
        return (
            row["id"],
            "retryable"
            if isinstance(exc, (ConnectionError, TimeoutError, URLError))
            and (not isinstance(exc, HTTPError) or exc.code >= 500)
            else "failed",
            {
                "error": type(exc).__name__ + ":" + str(exc)[:250],
                "products": products,
                "navigation_enabled": False,
            },
            [],
            time.perf_counter() - began,
        )


def reconstruct(database, url, graph, workers=2, limit=0):
    if not isinstance(graph, str) or not graph.strip():
        raise ValueError("graph_version_required")
    if not 1 <= workers <= 4:
        raise ValueError("worker_limit")
    if limit < 0:
        raise ValueError("invalid_batch_limit")
    with open(str(database) + ".runner.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _reconstruct(database, url, graph, workers, limit)


def _reconstruct(database, url, graph, workers, limit):
    store = CityStore(database)
    GraphHopperAdapter(url, graph).wait_ready()
    with store.db:
        store.db.execute(
            "UPDATE routes SET state='queued' WHERE state IN ('running','retryable')"
        )
        store.db.execute(
            "UPDATE routes SET state='queued' WHERE state NOT IN ('invalid','queued') AND (coalesce(graph_version,'')!=? OR coalesce(algorithm_version,'')!=?)",
            (graph, ALGORITHM),
        )
    annotations = store.meta("source_annotations") or {}
    started = time.perf_counter()
    done = 0
    pending = {}
    store.meta(
        "run",
        {
            "state": "running",
            "graph_version": graph,
            "algorithm": ALGORITHM,
            "started": time.time(),
            "pid": os.getpid(),
            "workers": workers,
        },
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            allowance = workers - len(pending)
            if limit:
                allowance = min(allowance, limit - done - len(pending))
            if allowance > 0:
                for row in store.db.execute(
                    "SELECT * FROM routes WHERE state='queued' ORDER BY id LIMIT ?",
                    (allowance,),
                ).fetchall():
                    store.db.execute(
                        "UPDATE routes SET state='running' WHERE id=?", (row["id"],)
                    )
                    record = dict(row)
                    record.update(annotations.get(str(row["id"]), {}))
                    pending[pool.submit(process, record, url, graph)] = row["id"]
            if not pending:
                break
            completed, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in completed:
                identifier, state, payload, geometry, elapsed = future.result()
                pending.pop(future)
                store.db.execute(
                    "INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        graph,
                        ALGORITHM,
                        state,
                        pack(payload),
                        elapsed,
                        time.time(),
                    ),
                )
                store.db.execute(
                    "UPDATE routes SET state=?,definition=?,rebuilt=?,rebuilt_display=?,graph_version=?,algorithm_version=?,updated=?,error=? WHERE id=?",
                    (
                        state,
                        pack(payload),
                        pack(geometry) if geometry else None,
                        pack(display_geometry(geometry)) if geometry else None,
                        graph,
                        ALGORITHM,
                        time.time(),
                        payload.get("error"),
                        identifier,
                    ),
                )
                store.link_controls(
                    identifier, graph, ALGORITHM, payload.get("products", [])
                )
                store.db.execute(
                    "INSERT INTO audit VALUES(?,?,?,?)",
                    (
                        time.time(),
                        state,
                        identifier,
                        json.dumps(
                            {
                                "elapsed_s": elapsed,
                                "requests": payload.get("routing_requests"),
                            }
                        ),
                    ),
                )
                if state == "retryable":
                    store.db.commit()
                    store.meta(
                        "run",
                        {
                            "state": "waiting_engine",
                            "graph_version": graph,
                            "algorithm": ALGORITHM,
                            "processed": done,
                            "updated": time.time(),
                        },
                    )
                    print(
                        "WAITING_ENGINE: progress saved; do not convert service outage into route failure",
                        flush=True,
                    )
                    return
                done += 1
                if done % 20 == 0:
                    store.db.commit()
                if done % 100 == 0:
                    print(
                        "RECONSTRUCT",
                        done,
                        "seconds",
                        round(time.perf_counter() - started, 1),
                        "states",
                        store.summary()["states"],
                        flush=True,
                    )
    store.db.commit()
    store.meta(
        "run",
        {
            "state": "batch_finished" if limit else "finished",
            "graph_version": graph,
            "algorithm": ALGORITHM,
            "processed": done,
            "elapsed_s": time.perf_counter() - started,
            "finished": time.time(),
            "workers": workers,
        },
    )
    print(
        "RECONSTRUCTION_COMPLETE",
        json.dumps(store.summary(), ensure_ascii=False),
        flush=True,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["ingest", "run", "status"])
    p.add_argument("--database", required=True)
    p.add_argument("--source")
    p.add_argument("--url", default="http://127.0.0.1:18989")
    p.add_argument("--graph-version")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()
    if a.action == "ingest":
        ingest(a.source, a.database)
    elif a.action == "run":
        reconstruct(a.database, a.url, a.graph_version, a.workers, a.limit)
    else:
        print(json.dumps(CityStore(a.database).summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
