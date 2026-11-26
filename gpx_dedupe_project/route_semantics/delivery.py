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

VERSION = "route-density-delivery-1"


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
        with db:
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


def select(
    database,
    output_dir,
    region="路线",
    threshold=10,
    fraction=0.2,
    minimum=500,
    node="node",
):
    if (
        not math.isfinite(threshold)
        or threshold < 0
        or not math.isfinite(fraction)
        or not 0 <= fraction <= 1
        or not math.isfinite(minimum)
        or minimum <= 0
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
            index_density(database, coverage)
            preview = root / "preview.next.html"
            export(
                database,
                coverage,
                preview,
                region=region,
                initial_policy={
                    "threshold": threshold,
                    "fraction": fraction,
                    "minimum": minimum,
                },
            )
            policy_file = root / "policy.json"
            subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("density_refresh.cjs")),
                    str(preview),
                    str(policy_file),
                    "--threshold",
                    str(threshold),
                    "--low-fraction",
                    str(fraction),
                    "--minimum-meters",
                    str(minimum),
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
            # Index page uses same final rule dynamically; all reliable geometries are available.
            preview.replace(root / "index.html")
            report.update(
                delivery_version=VERSION,
                region=region,
                delivery_elapsed_s=time.monotonic() - began,
                source_database=str(database),
                source_mode="read_only",
                dedup_enabled=False,
                exported_routes=geometry_count,
                artifacts=[
                    "coverage.sqlite3",
                    "report.json",
                    "run-state.json",
                    "decisions.csv",
                    "selected_routes.ndjson",
                    "routes.geojson",
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
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
