"""Self contained original-trace coverage viewer export."""

import argparse
import json
import math
import sqlite3
from pathlib import Path

from .city_store import CityStore, unpack
from .coverage import COS_STANDARD, RADIUS


def export(source, coverage, output):
    db = sqlite3.connect(f"file:{Path(coverage).resolve()}?mode=ro", uri=True)
    store = CityStore(source, initialize=False)
    try:

        def inverse(x, y):
            return [
                math.degrees(x / RADIUS / COS_STANDARD),
                math.degrees(math.asin(max(-1, min(1, y * COS_STANDARD / RADIUS)))),
            ]

        cells = []
        for size, x, y, count, length in db.execute(
            "SELECT size,x,y,route_count,total_length_m FROM cells"
        ):
            ring = [
                inverse((x + dx) * size, (y + dy) * size)
                for dx, dy in [(0, 0), (1, 0), (1, 1), (0, 1)]
            ]
            cells.append([size, x, y, count, length, ring])
        routes = []
        has_decisions = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='decisions'"
        ).fetchone()
        run_id = (
            db.execute(
                "SELECT max(id) FROM selection_runs WHERE report IS NOT NULL"
            ).fetchone()[0]
            if has_decisions
            else None
        )
        if run_id:
            report = json.loads(
                db.execute(
                    "SELECT report FROM selection_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            )
            count = db.execute("SELECT count(*) FROM routes").fetchone()[0]
            if report["total"] != count:
                raise ValueError("coverage grew: rerun selection before exporting")
        decisions = {}
        if run_id:
            for rid, kind, selected, reason, novel, sparse in db.execute(
                "SELECT route_id,geometry_kind,selected,reason,novel_length_m,sparse_length_m FROM decisions WHERE run_id=?",
                (run_id,),
            ):
                decisions[rid] = {
                    "kind": kind,
                    "selected": bool(selected),
                    "reason": reason,
                    "novel": novel,
                    "sparse": sparse,
                }
        for rid, length in db.execute("SELECT id,length_m FROM routes ORDER BY id"):
            row = store.db.execute(
                "SELECT coalesce(display,parts),coalesce(rebuilt_display,rebuilt) FROM routes WHERE id=?",
                (rid,),
            ).fetchone()
            contributions = list(
                db.execute(
                    "SELECT size,x,y,length_m,share_of_cell,share_of_route FROM contribution_metrics WHERE route_id=?",
                    (rid,),
                )
            )
            decision = decisions.get(
                rid,
                {
                    "kind": "original",
                    "selected": True,
                    "reason": "尚未执行覆盖选择",
                    "novel": 0,
                    "sparse": 0,
                },
            )
            rebuilt = (
                unpack(row[1]) if decision["kind"] == "rebuilt" and row[1] else None
            )
            routes.append(
                {
                    "id": rid,
                    "length": length,
                    "parts": unpack(row[0]),
                    "rebuilt": rebuilt,
                    "decision": decision,
                    "contributions": contributions,
                }
            )
        template = Path(__file__).with_name("coverage_view.html").read_text()
        gcj = next(
            line
            for line in Path(__file__)
            .with_name("city_view.html")
            .read_text()
            .splitlines()
            if line.startswith("function gcj(")
        )
        payload = json.dumps(
            {"cells": cells, "routes": routes},
            ensure_ascii=False,
            separators=(",", ":"),
        ).replace("<", "\u003c")
        destination = Path(output)
        temporary = destination.with_suffix(".tmp.html")
        temporary.write_text(
            template.replace("__DATA__", payload).replace("__GCJ__", gcj),
            encoding="utf8",
        )
        temporary.replace(destination)
        return {"routes": len(routes), "cells": len(cells), "output": str(output)}
    finally:
        db.close()
        store.db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--coverage", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    print(json.dumps(export(a.source, a.coverage, a.output), ensure_ascii=False))
