"""Resumable, isolated equal-area coverage experiment; never deletes routes."""

import argparse
import hashlib
import itertools
import json
import math
import sqlite3
import time
from pathlib import Path

from .city_store import CityStore, unpack
from .geometry import distance

VERSION = "coverage-1"
RADIUS = 6371000
COS_STANDARD = math.cos(math.radians(30))


def project(point):
    lon, lat = point[:2]
    if not (
        math.isfinite(lon)
        and math.isfinite(lat)
        and -180 <= lon <= 180
        and -90 <= lat <= 90
    ):
        raise ValueError("invalid WGS84 coordinate")
    return (
        RADIUS * math.radians(lon) * COS_STANDARD,
        RADIUS * math.sin(math.radians(lat)) / COS_STANDARD,
    )


def measure(parts, sizes=(100, 500), step=20, max_gap=1000):
    """Midpoint quadrature weighted by length, preserving segment boundaries.

    Repeated traversals contribute length but only one route per cell. Large
    observation gaps are excluded and explicitly reported, never connected.
    """
    cells = {}
    total = gaps = 0.0
    for points in parts:
        for a, b in itertools.pairwise(points):
            project(a)
            project(b)
            length = distance(a, b)
            if length > max_gap:
                gaps += length
                continue
            count = max(1, math.ceil(length / step))
            for i in range(count):
                t = (i + 0.5) / count
                x, y = project((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
                for size in sizes:
                    key = (size, math.floor(x / size), math.floor(y / size))
                    cells[key] = cells.get(key, 0) + length / count
            total += length
    return cells, total, gaps


def build(source, output, limit=300, sizes=(100, 500), step=20, max_gap=1000):
    if (
        limit < 1
        or step <= 0
        or max_gap <= 0
        or not sizes
        or any(s <= 0 for s in sizes)
    ):
        raise ValueError("positive limit, sampling step, gap and cell sizes required")
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("coverage output must be separate from source")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    store = CityStore(source, initialize=False)
    db = sqlite3.connect(output)
    try:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY,sha TEXT,length_m REAL,gap_m REAL,evidence_hash TEXT);
        CREATE TABLE IF NOT EXISTS contributions(route_id INTEGER,size INTEGER,x INTEGER,y INTEGER,length_m REAL,PRIMARY KEY(route_id,size,x,y));
        CREATE INDEX IF NOT EXISTS cell_routes ON contributions(size,x,y,route_id);
        """)
        source_id = [
            tuple(r)
            for r in store.db.execute(
                "SELECT id,size,mtime_ns FROM sources ORDER BY id"
            )
        ]
        config = json.dumps(
            {
                "version": VERSION,
                "source": str(Path(source).resolve()),
                "sources": source_id,
                "sizes": list(sizes),
                "step": step,
                "max_gap": max_gap,
            },
            sort_keys=True,
        )
        prior = db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        if prior and prior[0] != config:
            raise ValueError(
                "configuration/source changed: use a new coverage database"
            )
        db.execute("INSERT OR IGNORE INTO metadata VALUES('config',?)", (config,))
        db.commit()
        existing_count = db.execute("SELECT count(*) FROM routes").fetchone()[0]
        if existing_count > limit:
            raise ValueError(
                "limit is smaller than existing coverage; use a new database"
            )
        processed = skipped = 0
        started = time.monotonic()
        for row in store.db.execute(
            "SELECT id,content_sha256,parts FROM routes ORDER BY id LIMIT ?", (limit,)
        ):
            old = db.execute(
                "SELECT sha FROM routes WHERE id=?", (row["id"],)
            ).fetchone()
            if old:
                if old[0] != row["content_sha256"]:
                    raise ValueError("source row changed: use a new coverage database")
                skipped += 1
                continue
            parts = unpack(row["parts"])
            cells, length, gaps = measure(parts, sizes, step, max_gap)
            # Evidence hash describes the stored ordered parts, not independent users.
            evidence = hashlib.sha256(
                json.dumps(parts, separators=(",", ":")).encode()
            ).hexdigest()
            with db:
                if processed == 0:
                    db.execute(
                        "INSERT OR REPLACE INTO metadata VALUES('coverage_ready','false')"
                    )
                    # Derived coarse scales must be recomputed after index growth.
                    marks = ",".join("?" for _ in sizes)
                    db.execute(
                        f"DELETE FROM contributions WHERE size NOT IN ({marks})",
                        tuple(sizes),
                    )
                db.execute(
                    "INSERT INTO routes VALUES(?,?,?,?,?)",
                    (row["id"], row["content_sha256"], length, gaps, evidence),
                )
                db.executemany(
                    "INSERT INTO contributions VALUES(?,?,?,?,?)",
                    [(row["id"], *key, value) for key, value in cells.items()],
                )
            processed += 1
        with db:
            db.executescript("""
            DROP TABLE IF EXISTS cells;
            CREATE TABLE cells AS SELECT size,x,y,count(*) route_count,sum(length_m) total_length_m
            FROM contributions GROUP BY size,x,y;
            CREATE UNIQUE INDEX cell_key ON cells(size,x,y);
            """)
            report = {
                "version": VERSION,
                "requested_limit": limit,
                "total_indexed": db.execute("SELECT count(*) FROM routes").fetchone()[
                    0
                ],
                "processed": processed,
                "resumed": skipped,
                "elapsed_s": time.monotonic() - started,
                "complete_source": db.execute("SELECT count(*) FROM routes").fetchone()[
                    0
                ]
                == store.db.execute("SELECT count(*) FROM routes").fetchone()[0],
                "grid_only": True,
                "automatic_deletion": False,
            }
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('last_run',?)",
                (json.dumps(report),),
            )
        db.executescript("""
        DROP VIEW IF EXISTS contribution_metrics;
        CREATE VIEW contribution_metrics AS
        SELECT c.route_id,c.size,c.x,c.y,c.length_m,
               c.length_m / nullif(g.total_length_m,0) AS share_of_cell,
               c.length_m / nullif(r.length_m,0) AS share_of_route,
               g.route_count, g.route_count*1000000.0/(c.size*c.size) AS routes_per_km2
        FROM contributions c JOIN cells g USING(size,x,y) JOIN routes r ON r.id=c.route_id;
        DROP VIEW IF EXISTS route_metrics;
        CREATE VIEW route_metrics AS
        SELECT c.route_id,c.size,r.length_m,r.gap_m,
               sum(CASE WHEN g.route_count=1 THEN c.length_m ELSE 0 END) AS exclusive_grid_length_m,
               sum(CASE WHEN g.route_count>=10 THEN c.length_m ELSE 0 END)/nullif(r.length_m,0) AS dense_length_fraction
        FROM contributions c JOIN cells g USING(size,x,y) JOIN routes r ON r.id=c.route_id
        GROUP BY c.route_id,c.size;
        """)
        with db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('coverage_ready','true')"
            )
        return report
    finally:
        db.close()
        store.db.close()


def select(output, size=100, target=0.95):
    """Greedy grid coverage shortlist, NOT a semantic duplicate decision.

    Distinct structure may be absent from grid representation: all non-selected
    routes remain 'needs_path_comparison', never discarded automatically.
    """
    if not 0 < target <= 1:
        raise ValueError("target must be in (0,1]")
    db = sqlite3.connect(output)
    try:
        rows = db.execute(
            "SELECT route_id,x,y,length_m FROM contributions WHERE size=?", (size,)
        )
        routes = {}
        for rid, x, y, length in rows:
            routes.setdefault(rid, {})[(x, y)] = length
        covered = set()
        remaining = set(routes)
        selected = []
        universe = set().union(*(set(c) for c in routes.values())) if routes else set()
        while remaining and len(covered) < target * len(universe):
            # Bound each cell contribution: repeated laps cannot dominate selection.
            rid = max(
                remaining,
                key=lambda r: (
                    sum(min(v, size) for k, v in routes[r].items() if k not in covered),
                    -r,
                ),
            )
            novel = set(routes[rid]) - covered
            if not novel:
                break
            selected.append(rid)
            covered.update(novel)
            remaining.remove(rid)
        db.executescript(
            "DROP TABLE IF EXISTS shortlist; CREATE TABLE shortlist(route_id INTEGER PRIMARY KEY,status TEXT,rank INTEGER);"
        )
        ranks = {rid: i + 1 for i, rid in enumerate(selected)}
        with db:
            db.executemany(
                "INSERT INTO shortlist VALUES(?,?,?)",
                [
                    (
                        rid,
                        "coverage_representative"
                        if rid in ranks
                        else "needs_path_comparison",
                        ranks.get(rid),
                    )
                    for rid in routes
                ],
            )
        return {
            "selected": len(selected),
            "routes": len(routes),
            "covered_cells": len(covered),
            "total_cells": len(universe),
            "grid_only": True,
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="Total prefix size, not additional rows; resume is idempotent",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "index": build(args.source, args.output, args.limit),
                "selection": select(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
