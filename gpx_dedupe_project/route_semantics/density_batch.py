"""Isolated full-city 5km density processing; no routing requests."""

import argparse
import json
import os
import resource
import sqlite3
import time
from pathlib import Path

from . import coverage
from .city_store import CityStore
from .coverage_fast import measure


def choose(kind, length, gap, low, threshold=10, fraction=0.2):
    if length <= 0 or gap > 0:
        return False, "none", "原始观测需复核"
    keep = low / length + 1e-9 >= fraction and low + 1e-6 >= min(500, length)
    if not keep and kind == "candidate":
        return True, "rebuilt", "以高密度区为主，采用可靠重建"
    return (
        keep,
        "original",
        "低密度覆盖达到门槛，保留整条原始轨迹"
        if keep
        else "低密度覆盖不足，降低默认展示优先级",
    )


def run(source, output):
    store = CityStore(source, initialize=False)
    count = store.db.execute("SELECT count(*) FROM routes").fetchone()[0]
    store.db.close()
    began = time.monotonic()
    previous_measure, previous_version = coverage.measure, coverage.VERSION
    try:
        coverage.measure = measure
        coverage.VERSION = "coverage-vector-1"
        index = coverage.build(source, output, count, sizes=(5000,))
    finally:
        coverage.measure, coverage.VERSION = previous_measure, previous_version
    db = sqlite3.connect(output)
    store = CityStore(source, initialize=False)
    try:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS density_decisions(route_id INTEGER PRIMARY KEY,geometry_kind TEXT,selected INTEGER,low_length_m REAL,low_fraction REAL,reason TEXT);"
        )
        records = []
        stats = {
            t: {"original": 0, "rebuilt": 0, "hidden": 0, "review": 0}
            for t in [2, 5, 10, 20, 40, 80]
        }
        for rid, length, gap, sha in db.execute(
            "SELECT id,length_m,gap_m,sha FROM routes ORDER BY id"
        ):
            r = store.db.execute(
                "SELECT state,rebuilt IS NOT NULL,content_sha256 FROM routes WHERE id=?",
                (rid,),
            ).fetchone()
            if r is None or r[2] != sha:
                raise ValueError("source changed")
            kind = "candidate" if r[0] == "candidate" and r[1] else "original"
            cells = list(
                db.execute(
                    "SELECT c.length_m,g.route_count FROM contributions c JOIN cells g USING(size,x,y) WHERE c.route_id=? AND c.size=5000",
                    (rid,),
                )
            )
            for t, counters in stats.items():
                low = sum(v for v, n in cells if n <= t)
                keep, geometry, reason = choose(kind, length, gap, low, t)
                counters[
                    "review" if geometry == "none" else geometry if keep else "hidden"
                ] += 1
                if t == 10:
                    records.append(
                        (
                            rid,
                            geometry,
                            int(keep),
                            low,
                            low / length if length else 0,
                            reason,
                        )
                    )
        report = {
            "total": count,
            "complete": True,
            "grid_m": 5000,
            "threshold": 10,
            "min_low_fraction": 0.2,
            "min_low_m": 500,
            "index": index,
            "thresholds": stats,
            "elapsed_s": time.monotonic() - began,
        }
        with db:
            db.execute("DELETE FROM density_decisions")
            db.executemany("INSERT INTO density_decisions VALUES(?,?,?,?,?,?)", records)
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('density_report',?)",
                (json.dumps(report),),
            )
        Path(output).with_suffix(".report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report
    finally:
        store.db.close()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    run(a.source, a.output)
