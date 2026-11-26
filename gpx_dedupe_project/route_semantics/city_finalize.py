"""Normalize route definitions, archive revisions, verify immutable source bytes."""

import argparse
import fcntl
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

from .city_store import CityStore, pack, unpack


def finalize(database):
    # Share ownership with reconstruction; a graph/version change cannot race
    # with normalization or immutable result archival.
    with open(str(database) + ".runner.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _finalize(database)


def _finalize(database):
    store = CityStore(database)
    pending = store.db.execute(
        "SELECT count(*) FROM routes WHERE state IN ('queued','running','retryable')"
    ).fetchone()[0]
    if pending:
        print("FINALIZE_DEFERRED", pending, flush=True)
        return {"state": "pending", "count": pending}
    run = store.meta("run") or {}
    store.meta("run", {**run, "state": "finalizing"})
    store.db.executescript("""CREATE TABLE IF NOT EXISTS result_revisions(id TEXT PRIMARY KEY,route_id INTEGER,graph_version TEXT,algorithm_version TEXT,state TEXT,payload BLOB,created REAL);
    CREATE INDEX IF NOT EXISTS result_revision_route ON result_revisions(route_id,created);
    CREATE TABLE IF NOT EXISTS anchor_revisions(revision_id TEXT,part INTEGER,sequence INTEGER,anchor_id TEXT,lon REAL,lat REAL,relation TEXT,PRIMARY KEY(revision_id,part,sequence));""")
    with store.db:
        for prior in store.db.execute(
            "SELECT route_id,graph_version,algorithm_version,state,payload,created FROM results"
        ):
            context = json.dumps(
                [prior["route_id"], prior["graph_version"], prior["algorithm_version"]]
            ).encode()
            revision = hashlib.sha256(context + prior["payload"]).hexdigest()
            store.db.execute(
                "INSERT OR IGNORE INTO result_revisions VALUES(?,?,?,?,?,?,?)",
                (
                    revision,
                    prior["route_id"],
                    prior["graph_version"],
                    prior["algorithm_version"],
                    prior["state"],
                    prior["payload"],
                    prior["created"],
                ),
            )
    began = time.perf_counter()
    last = 0
    normalized = 0
    failure_reasons = Counter()
    product_modes = Counter()
    route_requests = 0
    while True:
        rows = store.db.execute(
            "SELECT * FROM routes WHERE id>? ORDER BY id LIMIT 100", (last,)
        ).fetchall()
        if not rows:
            break
        with store.db:
            for row in rows:
                last = row["id"]
                payload = unpack(row["definition"])
                if not payload:
                    continue
                route_requests += payload.get("routing_requests", 0)
                if row["state"] not in ("candidate", "review"):
                    reasons = {
                        product.get("diagnosis", {}).get("message", "unclassified")
                        for product in payload.get("products", [])
                        if product.get("mode") != "road_candidate"
                    }
                    if payload.get("error"):
                        reasons.add(payload["error"].split(":", 1)[0])
                    failure_reasons.update(reasons or {"unclassified"})
                product_modes.update(
                    product.get("mode", "unknown")
                    for product in payload.get("products", [])
                )
                if (
                    payload.get("route_semantic_id")
                    and payload.get("extraction_version") == row["algorithm_version"]
                    and payload.get("road_graph_version") == row["graph_version"]
                    and payload.get("geometry_constraints", {}).get(
                        "native_segment_continuity_tolerance_m"
                    )
                    == 0.05
                    and "ordered_segments" in payload
                ):
                    normalized += 1
                    continue
                old = row["definition"]
                context = json.dumps(
                    [last, row["graph_version"], row["algorithm_version"]]
                ).encode()
                old_revision = hashlib.sha256(context + old).hexdigest()
                if not payload.get("definitions"):
                    source_parts = unpack(row["parts"]) or []
                    fallback = []
                    definitions = []
                    for source_part, points in enumerate(source_parts):
                        if len(points) < 2:
                            continue
                        a = {
                            "coordinate": points[0],
                            "kind": "endpoint",
                            "evidence_index": 0,
                        }
                        b = {
                            "coordinate": points[-1],
                            "kind": "endpoint",
                            "evidence_index": len(points) - 1,
                        }
                        fallback.append({"controls": [a, b]})
                        definitions.append(
                            {
                                "source_part": source_part,
                                "start_anchor": a,
                                "end_anchor": b,
                                "ordered_segments": [
                                    {
                                        "from": a,
                                        "to": b,
                                        "mode": "foot",
                                        "mandatory_edges": [],
                                        "forbidden_edges": [],
                                        "constraint_status": "unresolved_source_observation",
                                    }
                                ],
                                "status": "extraction_incomplete",
                            }
                        )
                    payload["definitions"] = definitions
                    payload["extraction_status"] = "minimal_observed_endpoints_only"
                    store.link_controls(
                        last, row["graph_version"], row["algorithm_version"], fallback
                    )
                payload["schema_version"] = "city-route-1"
                payload["source_sha256"] = row["content_sha256"]
                payload["road_graph_version"] = row["graph_version"]
                payload["algorithm_version"] = row["algorithm_version"]
                payload["source_parts"] = (
                    len(unpack(row["parts"]) or [])
                    if "source_parts" not in payload
                    else payload["source_parts"]
                )

                store.db.execute(
                    "INSERT OR IGNORE INTO result_revisions VALUES(?,?,?,?,?,?,?)",
                    (
                        old_revision,
                        last,
                        row["graph_version"],
                        row["algorithm_version"],
                        row["state"],
                        old,
                        row["updated"],
                    ),
                )
                controls = [
                    dict(r)
                    for r in store.db.execute(
                        "SELECT part,sequence,anchor_id,lon,lat,relation FROM route_anchors WHERE route_id=? AND graph_version=? AND algorithm_version=? ORDER BY part,sequence",
                        (last, row["graph_version"], row["algorithm_version"]),
                    )
                ]
                reference = {(r["part"], r["sequence"]): r for r in controls}
                semantic_id = (
                    "rs:"
                    + hashlib.sha256(
                        (
                            row["source_id"]
                            + ":"
                            + str(row["source_offset"])
                            + ":"
                            + row["content_sha256"]
                        ).encode()
                    ).hexdigest()[:24]
                )
                ordered = []
                for part, definition in enumerate(payload.get("definitions", [])):
                    sequence = []
                    for i, segment in enumerate(definition.get("ordered_segments", [])):
                        pair = []
                        for sequence_id, anchor in (
                            (i, segment["from"]),
                            (i + 1, segment["to"]),
                        ):
                            ref = reference.get((part, sequence_id))
                            coordinate = anchor["coordinate"]
                            identifier = (
                                "control:"
                                + hashlib.sha256(
                                    json.dumps(
                                        [semantic_id, part, sequence_id, coordinate]
                                    ).encode()
                                ).hexdigest()[:24]
                            )
                            if (
                                sequence_id == 0
                                and ordered
                                and ordered[-1]["source_part"]
                                == definition["source_part"]
                                and ordered[-1]["to"]["coordinate"] == coordinate
                            ):
                                identifier = ordered[-1]["to_anchor_id"]
                            anchor["id"] = identifier
                            anchor["coordinate_role"] = "planning_constraint"
                            anchor["coordinate_origin"] = (
                                "observed_endpoint"
                                if anchor.get("kind") == "endpoint"
                                else "robust_estimate"
                            )
                            anchor["evidence_index_basis"] = (
                                "local_window_filtered_arclength_samples"
                                if anchor.get("kind") != "endpoint"
                                else "window_endpoint"
                            )
                            if ref and ref["relation"] == "nearby_entity_candidate":
                                anchor["entity_candidate_id"] = ref["anchor_id"]
                                anchor["entity_relation"] = "nearby_unconfirmed"
                            pair.append(identifier)
                        segment.update(
                            from_anchor_id=pair[0],
                            to_anchor_id=pair[1],
                            via_anchor_ids=[],
                            corridor_id="observed:"
                            + row["content_sha256"][:20]
                            + ":"
                            + str(part),
                            source_part=definition["source_part"],
                            break_before=bool(
                                i == 0
                                and ordered
                                and ordered[-1]["source_part"]
                                != definition["source_part"]
                            ),
                            road_constraint_resolution="window_level_evidence",
                        )
                        sequence.append(segment)
                    definition["window_index"] = part
                    definition["ordered_segments"] = sequence
                    if sequence:
                        definition["start_anchor"] = sequence[0]["from"]
                        definition["end_anchor"] = sequence[-1]["to"]
                    ordered.extend(sequence)
                observed_cells = sorted(
                    {
                        (int(p[0] * 100), int(p[1] * 100))
                        for part in unpack(row["display"])
                        for p in part
                    }
                )
                payload.update(
                    route_semantic_id=semantic_id,
                    source_trace_ids=[
                        "trace:"
                        + hashlib.sha256(
                            (row["source_id"] + str(row["source_offset"])).encode()
                        ).hexdigest()[:24]
                    ],
                    ordered_segments=ordered,
                    geographical_fingerprint=hashlib.sha256(
                        json.dumps(observed_cells).encode()
                    ).hexdigest(),
                    observed_grid_ids=[
                        "geo:grid:.01:" + str(a) + ":" + str(b)
                        for a, b in observed_cells
                    ],
                    reconstruction_status=row["state"],
                    distance={
                        "observed_m": row["distance_m"],
                        "estimated_m": None,
                        "reconstructed_m": None,
                    },
                    confidence=None,
                    independent_evidence_count=None,
                    dedup_executed=False,
                    navigation_enabled=False,
                )
                definitions = payload.get("definitions", [])
                payload["start_anchor"] = (
                    definitions[0].get("start_anchor") if definitions else None
                )
                payload["end_anchor"] = (
                    definitions[-1].get("end_anchor") if definitions else None
                )
                payload["extraction_version"] = row["algorithm_version"]
                payload.setdefault("road_type_preferences", {})
                payload.setdefault("mandatory_edges", [])
                payload.setdefault("forbidden_edges", [])
                payload["elevation_gain"] = None
                payload["planning_mode"] = "foot"
                payload["observed_activity_type"] = None
                payload["source_authorization_status"] = "unknown"
                from .geometry import distance

                payload["shape_type"] = (
                    "multi_segment"
                    if payload.get("source_parts", 1) > 1
                    else "closed"
                    if payload["start_anchor"]
                    and payload["end_anchor"]
                    and distance(
                        payload["start_anchor"]["coordinate"],
                        payload["end_anchor"]["coordinate"],
                    )
                    <= 35
                    else "open"
                )
                payload["shape_type_basis"] = "endpoint_closure_only"
                payload["geometry_constraints"] = {
                    "evidence": "immutable_source_windows",
                    "raw_trace_fallback": False,
                    "native_segment_continuity_tolerance_m": 0.05,
                }
                if row["state"] in ("candidate", "review"):
                    from .geometry import length

                    payload["distance"]["reconstructed_m"] = sum(
                        length(p) for p in (unpack(row["rebuilt"]) or [])
                    )
                if row["state"] != "candidate":
                    payload["core_fingerprint"] = None
                    payload["fingerprint_basis"] = "road_semantics_unconfirmed"
                else:
                    payload["fingerprint_basis"] = "validated_candidate_road_ids"
                payload["version_fingerprint"] = hashlib.sha256(
                    json.dumps(
                        [
                            semantic_id,
                            payload.get("algorithm_version"),
                            payload.get("road_graph_version"),
                            definitions,
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                final = pack(payload)
                revision = hashlib.sha256(context + final).hexdigest()
                store.db.execute(
                    "INSERT OR IGNORE INTO result_revisions VALUES(?,?,?,?,?,?,?)",
                    (
                        revision,
                        last,
                        row["graph_version"],
                        row["algorithm_version"],
                        row["state"],
                        final,
                        time.time(),
                    ),
                )
                for ref in controls:
                    store.db.execute(
                        "INSERT OR IGNORE INTO anchor_revisions VALUES(?,?,?,?,?,?,?)",
                        (
                            revision,
                            ref["part"],
                            ref["sequence"],
                            ref["anchor_id"],
                            ref["lon"],
                            ref["lat"],
                            ref["relation"],
                        ),
                    )
                store.db.execute(
                    "UPDATE routes SET definition=? WHERE id=?", (final, last)
                )
                store.db.execute(
                    "UPDATE results SET payload=? WHERE route_id=? AND graph_version=? AND algorithm_version=?",
                    (final, last, row["graph_version"], row["algorithm_version"]),
                )
                normalized += 1
        if normalized % 1000 == 0:
            print("DEFINITIONS_ARCHIVED", normalized, flush=True)
    integrity = []
    for source in store.db.execute("SELECT * FROM sources").fetchall():
        expected = dict(
            store.db.execute(
                "SELECT source_offset,content_sha256 FROM routes WHERE source_id=?",
                (source["id"],),
            ).fetchall()
        )
        checked = 0
        mismatches = []
        whole = hashlib.sha256()
        path = Path(source["path"])
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw = stream.readline(32 * 1024 * 1024 + 1)
                if not raw:
                    break
                whole.update(raw)
                checked += 1
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("source_record_limit_during_verification")
                if (
                    expected.get(offset) != hashlib.sha256(raw).hexdigest()
                    and len(mismatches) < 20
                ):
                    mismatches.append(offset)
        integrity.append(
            {
                "source_id": source["id"],
                "records_checked": checked,
                "file_sha256": whole.hexdigest(),
                "record_hashes_match_ingestion": not mismatches
                and checked == len(expected),
                "mismatch_offsets": mismatches,
                "bytes": path.stat().st_size,
            }
        )
    store.meta("source_integrity", integrity)
    timings = sorted(r[0] for r in store.db.execute("SELECT elapsed_s FROM results"))
    summary = store.summary()
    report = {
        "schema_version": "city-delivery-1",
        "experimental": True,
        "total": summary["total"],
        "states": summary["states"],
        "anchors": summary["anchor_count"],
        "shared_entity_candidates": summary["shared_entity_count"],
        "definition_records": normalized,
        "source_point_count": store.db.execute(
            "SELECT sum(point_count) FROM routes"
        ).fetchone()[0],
        "routing_requests": route_requests,
        "failed_record_reason_counts": dict(failure_reasons),
        "failure_reason_count_basis": "one count per distinct reason per rejected record; a record may have multiple reasons",
        "window_product_modes": dict(product_modes),
        "immutable_result_revisions": store.db.execute(
            "SELECT count(*) FROM result_revisions"
        ).fetchone()[0],
        "result_compute_sum_s": sum(timings),
        "p95_compute_record_s": timings[math.ceil(len(timings) * 0.95) - 1]
        if timings
        else None,
        "source_integrity": integrity,
        "resources": store.meta("resource_limits"),
        "graph": store.meta("graph_info"),
        "coverage": store.meta("graph_coverage"),
        "final_batch_records": run.get("processed"),
        "final_batch_wall_s": run.get("elapsed_s"),
        "final_batch_routes_per_second": run.get("processed", 0)
        / max(run.get("elapsed_s", 1), 0.001),
        "finalization_s": time.perf_counter() - began,
        "navigation_enabled": False,
        "dedup_executed": False,
        "limitations": [
            "POI associations are proximity candidates, not verified visits",
            "anonymous controls remain route-private",
            "directional road evidence is window-level; no claim of complete per-leg hard constraints",
            "confidence uncalibrated; no independent-origin usage counts",
            "failed or resource-limited routes are retained and not replaced by original GPS",
        ],
    }
    target = Path(database).with_name("delivery-report.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    state = (
        "finished"
        if all(x["record_hashes_match_ingestion"] for x in integrity)
        else "source_integrity_error"
    )
    store.meta(
        "run",
        {
            **run,
            "state": state,
            "normalized_routes": normalized,
            "verified": time.time(),
            "report": str(target),
        },
    )
    print(
        "CITY_DELIVERY",
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("source_integrity", "graph", "resources", "limitations")
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    a = p.parse_args()
    finalize(a.database)
