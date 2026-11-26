"""On-demand native planning from stored ordered controls, not fresh extraction."""

import argparse
import hashlib
import json
import os
import resource
import time
from pathlib import Path

os.environ["OPENBLAS_NUM_THREADS"] = "1"
from .adapters import GraphHopperAdapter, UnsupportedConstraint
from .city_pipeline import windows
from .city_store import CityStore, unpack
from .failure_diagnosis import diagnose
from .geometry import distance
from .sparse_routing import quality, stable_evidence


def rebuild(
    database, identifier, graph, url="http://127.0.0.1:18989", force=False, adapter=None
):
    if not isinstance(graph, str) or not graph.strip():
        raise ValueError("graph_version_required")
    store = CityStore(database, initialize=False)
    row = store.db.execute("SELECT * FROM routes WHERE id=?", (identifier,)).fetchone()
    store.db.close()
    if row is None:
        raise ValueError("route_not_found")
    definition = unpack(row["definition"])
    if not definition or not definition.get("definitions"):
        raise ValueError("route_definition_not_available")
    constraints = ("mandatory_edges", "forbidden_edges", "road_type_preferences")
    if any(definition.get(key) for key in constraints) or any(
        segment.get(key)
        for window in definition["definitions"]
        for segment in window.get("ordered_segments", [])
        for key in constraints
    ):
        raise UnsupportedConstraint(
            "stored_hard_or_cost_constraints_not_supported_by_native_adapter"
        )
    began = time.perf_counter()
    parts = []
    cached = (
        not force
        and row["graph_version"] == graph
        and row["state"] in ("candidate", "review")
        and row["rebuilt"] is not None
    )
    requests = 0
    state = row["state"]
    errors = []
    if cached:
        parts = unpack(row["rebuilt"])
    else:
        source = unpack(row["parts"])
        references = [
            (part, window)
            for part, points in enumerate(source)
            for window in windows(points)
        ]
        if len(references) != len(definition["definitions"]):
            raise ValueError("incomplete_definition_requires_extraction_review")
        engine = adapter or GraphHopperAdapter(url, graph)
        output = {}
        state = "candidate"
        for segment, (source_part, reference) in zip(
            definition["definitions"], references
        ):
            edges = segment.get("ordered_segments", [])
            if not edges:
                raise ValueError("missing_ordered_constraints")
            controls = [segment["start_anchor"]["coordinate"]] + [
                e["to"]["coordinate"] for e in edges
            ]
            if requests >= 64 or time.perf_counter() - began > 90:
                raise ValueError("on_demand_resource_limit")
            planned = engine.route(controls, "foot")
            requests += 1
            geometry = planned["geometry"]["coordinates"]
            passed, metrics, _ = quality(stable_evidence(reference), geometry)
            diagnosis = diagnose(
                {
                    "mode": "road_candidate" if passed else "road_unavailable",
                    "metrics": metrics,
                }
            )
            if not passed and not diagnosis["review_preview"]:
                state = "failed"
                errors.append(diagnosis)
                continue
            if not passed and state != "failed":
                state = "review"
            previous = output.setdefault(source_part, [])
            if previous and distance(previous[-1], geometry[0]) > 0.05:
                state = "failed"
                errors.append({"message": "native_boundary_not_continuous"})
                continue
            previous.extend(geometry if not previous else geometry[1:])
        if state != "failed":
            parts = [output[i] for i in sorted(output)]
    properties = {
        "route_id": identifier,
        "state": state,
        "road_graph_version": graph,
        "source_sha256": row["content_sha256"],
        "used_stored_definition": True,
        "definition_version": definition.get("version_fingerprint"),
        "definition_sha256": hashlib.sha256(row["definition"]).hexdigest(),
        "planning_mode": "foot",
        "navigation_enabled": False,
        "dedup_executed": False,
    }
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "MultiLineString", "coordinates": parts},
            }
        ]
        if parts
        else [],
        "metadata": {
            **properties,
            "geometry_from_cache": bool(cached),
            "routing_requests": requests,
            "elapsed_s": time.perf_counter() - began,
            "errors": errors,
            "raw_trace_fallback": False,
        },
    }


def main():
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--route-id", required=True, type=int)
    p.add_argument("--graph-version", required=True)
    p.add_argument("--url", default="http://127.0.0.1:18989")
    p.add_argument("--force", action="store_true")
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = rebuild(a.database, a.route_id, a.graph_version, a.url, a.force)
    Path(a.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))
    print(json.dumps(result["metadata"], ensure_ascii=False))


if __name__ == "__main__":
    main()
