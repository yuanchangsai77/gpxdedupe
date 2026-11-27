"""Delivery entry: existing route DB → full density → final policy → exports.

Linux/WSL. Uses a local Node executable for the same final rule as the viewer.
No routing, external requests, or writes to the input database.
"""

import argparse
import csv
import fcntl
import json
import math
import os
import resource
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .city_store import CityStore, unpack
from .coverage_view import export
from .density_batch import run as index_density
from .overlap_index import run as index_overlap

VERSION = "route-density-delivery-2"


def apply_policy(database, policy):
    """Validate complete row set before one transaction replaces final decisions."""
    db = sqlite3.connect(database)
    try:
        expected = {r[0] for r in db.execute("SELECT id FROM routes")}
        records = policy.get("records", [])
        if (
            len(records) != len(expected)
            or {r["id"] for r in records} != expected
            or policy.get("total") != len(expected)
        ):
            raise ValueError("policy route set mismatch")
        for r in records:
            if (
                r["kind"] not in ("original", "rebuilt", "none")
                or type(r["keep"]) is not bool
            ):
                raise ValueError("invalid decision")
            if r["kind"] == "none" and r["keep"]:
                raise ValueError("unreviewed route cannot be selected")
            if (
                r["lowLength"] < 0
                or not math.isfinite(r["lowLength"])
                or not math.isfinite(r["fraction"])
                or not 0 <= r["fraction"] <= 1 + 1e-8
            ):
                raise ValueError("invalid contribution")
        previous = db.execute(
            "SELECT value FROM metadata WHERE key='density_report'"
        ).fetchone()
        report = json.loads(previous[0]) if previous else {}
        report.update(
            policy=policy["policy"],
            total=policy["total"],
            threshold=policy["threshold"],
            min_low_fraction=policy["min_low_fraction"],
            min_low_m=policy["min_low_m"],
            thresholds=policy["thresholds"],
            complete=True,
        )
        report["overlap"] = policy.get("overlap", {"enabled": False, "mode": "off"})
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS overlap_metrics(route_id INTEGER PRIMARY KEY,payload TEXT)"
            )
            db.execute("DELETE FROM overlap_metrics")
            db.executemany(
                "INSERT INTO overlap_metrics VALUES(?,?)",
                [
                    (r["id"], json.dumps(r["coverage"], ensure_ascii=False))
                    for r in records
                    if r.get("coverage")
                ],
            )
            db.execute("DELETE FROM density_decisions")
            db.executemany(
                "INSERT INTO density_decisions VALUES(?,?,?,?,?,?)",
                [
                    (
                        r["id"],
                        r["kind"],
                        int(r["keep"]),
                        r["lowLength"],
                        r["fraction"],
                        r["reason"],
                    )
                    for r in records
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('density_report',?)",
                (json.dumps(report, ensure_ascii=False),),
            )
        return report
    finally:
        db.close()


def export_geometries(source, records, output):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("geometry output must differ from source")
    store = CityStore(source, initialize=False)
    destination = Path(output)
    temporary = destination.with_suffix(".next.geojson")
    count = 0
    try:
        with temporary.open("w", encoding="utf8") as stream:
            stream.write('{"type":"FeatureCollection","features":[')
            for record in records:
                if not record["keep"]:
                    continue
                row = store.db.execute(
                    "SELECT state,parts,rebuilt,graph_version,algorithm_version,content_sha256 FROM routes WHERE id=?",
                    (record["id"],),
                ).fetchone()
                if row is None:
                    raise ValueError("source route missing")
                kind = record["kind"]
                if kind == "rebuilt":
                    if row["state"] != "candidate" or not row["rebuilt"]:
                        raise ValueError("reconstruction no longer reliable")
                    parts = unpack(row["rebuilt"])
                elif kind == "original":
                    parts = unpack(row["parts"])
                else:
                    raise ValueError("invalid selected geometry kind")
                if not parts or any(len(part) < 2 for part in parts):
                    raise ValueError("selected route has no usable geometry")
                feature = {
                    "type": "Feature",
                    "geometry": {"type": "MultiLineString", "coordinates": parts},
                    "properties": {
                        "route_id": record["id"],
                        "geometry_kind": kind,
                        "reason": record["reason"],
                        "source_sha256": row["content_sha256"],
                        "navigation_verified": False,
                        "road_graph_version": row["graph_version"]
                        if kind == "rebuilt"
                        else None,
                        "algorithm_version": row["algorithm_version"]
                        if kind == "rebuilt"
                        else None,
                    },
                }
                if count:
                    stream.write(",")
                json.dump(
                    feature,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                count += 1
            stream.write("]}")
        temporary.replace(destination)
        return count
    finally:
        store.db.close()


def write_run_state(root, state, **details):
    temporary = root / "run-state.next.json"
    temporary.write_text(
        json.dumps(
            {"state": state, "pid": os.getpid(), "updated": time.time(), **details},
            ensure_ascii=False,
            indent=2,
        )
    )
    temporary.replace(root / "run-state.json")


def reusable_density_report(source, coverage):
    if not coverage.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{coverage.resolve()}?mode=ro", uri=True) as db:
            meta = {
                k: json.loads(v)
                for k, v in db.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('config','coverage_ready','last_run','density_report')"
                )
            }
            cfg = meta.get("config", {})
            if (
                cfg.get("version") != "coverage-vector-1"
                or cfg.get("source") != str(source)
                or cfg.get("sizes") != [5000]
                or cfg.get("step") != 20
                or cfg.get("max_gap") != 1000
            ):
                return None
            if not meta.get("coverage_ready") or not meta.get("last_run", {}).get(
                "complete_source"
            ):
                return None
            db.execute(
                "ATTACH DATABASE ? AS evidence", ("file:" + str(source) + "?mode=ro",)
            )
            count = db.execute("SELECT count(*) FROM evidence.routes").fetchone()[0]
            if (
                db.execute("SELECT count(*) FROM routes").fetchone()[0] != count
                or db.execute("SELECT count(*) FROM density_decisions").fetchone()[0]
                != count
            ):
                return None
            if db.execute(
                "SELECT 1 FROM routes r LEFT JOIN evidence.routes e ON r.id=e.id WHERE r.sha IS NOT e.content_sha256 LIMIT 1"
            ).fetchone():
                return None
            return meta.get("density_report")
    except (sqlite3.Error, ValueError, KeyError):
        return None


def select(
    database,
    output_dir,
    region="路线",
    threshold=10,
    fraction=0.2,
    minimum=500,
    node="node",
    overlap_mode="compact",
    overlap_fraction=0.5,
):
    if (
        not math.isfinite(threshold)
        or threshold < 0
        or not math.isfinite(fraction)
        or not 0 <= fraction <= 1
        or not math.isfinite(minimum)
        or minimum <= 0
        or overlap_mode not in ("off", "compact", "coverage")
        or not math.isfinite(overlap_fraction)
        or not 0 <= overlap_fraction <= 1
    ):
        raise ValueError("invalid policy limits")
    database = Path(database).resolve()
    root = Path(output_dir).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if root == database.parent:
        raise ValueError("use a separate delivery directory")
    if not shutil.which(node):
        raise ValueError("Node executable not found; install Node or pass --node")
    root.mkdir(parents=True, exist_ok=True)
    coverage = root / "coverage.sqlite3"
    if database == coverage:
        raise ValueError("source must differ from output")
    began = time.monotonic()
    with (root / "delivery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_run_state(root, "running", source_database=str(database))
        try:
            cached = reusable_density_report(database, coverage)
            if cached is None:
                index_density(database, coverage)
            overlap_path = None
            if overlap_mode != "off":
                overlap_path = root / "overlap-index.json"
                index_overlap(database, overlap_path)
            preview = root / "preview.next.html"
            policy_input = root / "policy-input.json"
            export(
                database,
                coverage,
                preview,
                region=region,
                overlap=overlap_path,
                policy_output=policy_input,
                initial_policy={
                    "threshold": threshold,
                    "fraction": fraction,
                    "minimum": minimum,
                    "overlapMode": overlap_mode,
                    "overlapFraction": overlap_fraction,
                    "overlapEnabled": overlap_mode != "off",
                },
            )
            policy_file = root / "policy.json"
            subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("density_refresh.cjs")),
                    str(policy_input),
                    str(policy_file),
                    "--threshold",
                    str(threshold),
                    "--low-fraction",
                    str(fraction),
                    "--minimum-meters",
                    str(minimum),
                    "--overlap-mode",
                    overlap_mode,
                    "--overlap-fraction",
                    str(overlap_fraction),
                ],
                check=True,
            )
            policy = json.loads(policy_file.read_text())
            report = apply_policy(coverage, policy)
            rows = policy["records"]
            with (root / "decisions.csv").open(
                "w", newline="", encoding="utf-8-sig"
            ) as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "route_id",
                        "selected",
                        "geometry_kind",
                        "low_length_m",
                        "low_fraction",
                        "reason",
                        "unique_fraction",
                        "unique_m",
                        "longest_unique_m",
                        "protected_reason",
                        "covered_by",
                    ]
                )
                writer.writerows(
                    (
                        r["id"],
                        int(r["keep"]),
                        r["kind"],
                        r["lowLength"],
                        r["fraction"],
                        r["reason"],
                        (r.get("coverage") or {}).get("unique_fraction", ""),
                        (r.get("coverage") or {}).get("unique_m", ""),
                        (r.get("coverage") or {}).get("longest_unique_m", ""),
                        (r.get("coverage") or {}).get("protected_reason", ""),
                        json.dumps((r.get("coverage") or {}).get("covered_by", [])),
                    )
                    for r in rows
                )
            with (root / "selected_routes.ndjson").open("w", encoding="utf8") as f:
                for r in rows:
                    if r["keep"]:
                        f.write(
                            json.dumps(
                                {
                                    "route_id": r["id"],
                                    "geometry_kind": r["kind"],
                                    "reason": r["reason"],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
            geometry_count = export_geometries(database, rows, root / "routes.geojson")
            from .final_view import export as export_final_view

            export_final_view(
                root / "routes.geojson", root / "final-routes.html", region
            )
            # Index page uses same final rule dynamically; all reliable geometries are available.
            preview.replace(root / "index.html")
            report.update(
                delivery_version=VERSION,
                region=region,
                delivery_elapsed_s=time.monotonic() - began,
                source_database=str(database),
                source_mode="read_only",
                density_cache_reused=cached is not None,
                dedup_enabled=False,
                exported_routes=geometry_count,
                artifacts=[
                    "coverage.sqlite3",
                    "report.json",
                    "run-state.json",
                    "decisions.csv",
                    "selected_routes.ndjson",
                    "routes.geojson",
                    "final-routes.html",
                    "index.html",
                ],
            )
            temporary = root / "report.next.json"
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            temporary.replace(root / "report.json")
            write_run_state(root, "succeeded", elapsed_s=time.monotonic() - began)
            return report
        except BaseException as exc:
            write_run_state(
                root, "failed", error_type=type(exc).__name__, error=str(exc)[:1000]
            )
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--region", default="路线")
    p.add_argument("--threshold", type=float, default=10)
    p.add_argument("--low-fraction", type=float, default=0.2)
    p.add_argument("--minimum-meters", type=float, default=500)
    p.add_argument("--node", default="node")
    p.add_argument(
        "--overlap-mode", choices=["compact", "coverage", "off"], default="compact"
    )
    p.add_argument("--overlap-fraction", type=float, default=0.5)
    a = p.parse_args()
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    print(
        json.dumps(
            select(
                a.database,
                a.output_dir,
                a.region,
                a.threshold,
                a.low_fraction,
                a.minimum_meters,
                a.node,
                a.overlap_mode,
                a.overlap_fraction,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
