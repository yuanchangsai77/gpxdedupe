"""Self contained original-trace coverage viewer export."""

import argparse
import html
import json
import math
import sqlite3
from pathlib import Path

from .city_store import CityStore, unpack
from .coverage import COS_STANDARD, RADIUS


def export(
    source,
    coverage,
    output,
    dedup=None,
    region="成都",
    initial_policy=None,
    overlap=None,
    policy_output=None,
):
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
        policy_routes = []
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
        density_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='density_decisions'"
        ).fetchone()
        if density_table:
            for rid, kind, selected, low, fraction, reason in db.execute(
                "SELECT * FROM density_decisions"
            ):
                decisions[rid] = {
                    "kind": kind,
                    "selected": bool(selected),
                    "reason": reason,
                    "novel": 0,
                    "sparse": low,
                }

        dedup_members = {}
        if dedup:
            with sqlite3.connect(
                f"file:{Path(dedup).resolve()}?mode=ro", uri=True
            ) as dedup_db:
                run = dedup_db.execute(
                    "SELECT id,report FROM runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if not run:
                    raise ValueError("no completed dedup run")
                from .rebuilt_dedup import source_snapshot

                selected_ids = {
                    rid
                    for rid, decision in decisions.items()
                    if decision["selected"] and decision["kind"] == "rebuilt"
                }
                report = json.loads(run[1])
                if report.get("source_snapshot") != source_snapshot(
                    store, selected_ids
                ):
                    raise ValueError("dedup result stale: rerun dedup before export")
                for rid, canonical, group, rank in dedup_db.execute(
                    "SELECT route_id,canonical_id,group_id,rank FROM members WHERE run_id=?",
                    (run[0],),
                ):
                    dedup_members[rid] = {
                        "canonical": canonical,
                        "group": group,
                        "rank": rank,
                    }

        def display_parts(blob, precision=5):
            return [
                [[round(p[0], precision), round(p[1], precision)] for p in part]
                for part in (unpack(blob) or [])
            ]

        for rid, length in db.execute("SELECT id,length_m FROM routes ORDER BY id"):
            row = store.db.execute(
                "SELECT coalesce(display,parts),rebuilt,state FROM routes WHERE id=?",
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
                display_parts(row[1], precision=6)
                if row[2] == "candidate" and row[1]
                else None
            )
            routes.append(
                {
                    "id": rid,
                    "length": length,
                    "parts": display_parts(row[0]),
                    "rebuilt": rebuilt,
                    "reliableRebuilt": row[2] == "candidate" and bool(row[1]),
                    "decision": decision,
                    "contributions": contributions,
                    "dedup": dedup_members.get(rid),
                }
            )
            policy_routes.append(
                {
                    "id": rid,
                    "length": length,
                    "rebuilt": [[]] if rebuilt else None,
                    "reliableRebuilt": row[2] == "candidate" and bool(row[1]),
                    "decision": dict(decision),
                    "contributions": [c for c in contributions if c[0] == 5000],
                }
            )
        if initial_policy is None:
            metadata = db.execute(
                "SELECT value FROM metadata WHERE key='density_report'"
            ).fetchone()
            report = json.loads(metadata[0]) if metadata else {}
            initial_policy = {
                "threshold": report.get("threshold", 10),
                "fraction": report.get("min_low_fraction", 0.2),
                "minimum": report.get("min_low_m", 500),
            }
        overlap_data = None
        if overlap:
            from .rebuilt_dedup import source_snapshot

            overlap_data = json.loads(Path(overlap).read_text())
            if overlap_data.get("source_snapshot") != source_snapshot(
                store, set(overlap_data["route_ids"])
            ):
                raise ValueError("overlap index stale: rerun overlap_index")
        initial_policy = dict(initial_policy)
        initial_policy.setdefault("overlapMode", "compact")
        initial_policy.setdefault("overlapFraction", 0.5)
        initial_policy.setdefault("overlapEnabled", bool(overlap_data))
        template = Path(__file__).with_name("coverage_view.html").read_text()
        gcj = next(
            line
            for line in Path(__file__)
            .with_name("city_view.html")
            .read_text()
            .splitlines()
            if line.startswith("function gcj(")
        )
        payload = {
            "cells": cells,
            "routes": routes,
            "complete": bool(density_table),
            "region": region,
            "initialPolicy": initial_policy,
            "overlap": overlap_data,
        }
        if policy_output:
            policy_path = Path(policy_output)
            if policy_path.resolve() in {
                Path(source).resolve(),
                Path(coverage).resolve(),
                Path(output).resolve(),
            }:
                raise ValueError("policy output must differ from inputs and view")
            compact = {
                "cells": [c[:5] for c in cells if c[0] == 5000],
                "routes": policy_routes,
                "overlap": overlap_data,
                "initialPolicy": initial_policy,
            }
            policy_path.write_text(
                json.dumps(
                    compact, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
            )
        destination = Path(output)
        temporary = destination.with_suffix(".tmp.html")
        template = template.replace("__REGION_TITLE__", html.escape(region)).replace(
            "__GCJ__", gcj
        )
        for marker, filename in (
            ("__FINAL_POLICY__", "final_policy.js"),
            ("__OVERLAP_POLICY__", "overlap_policy.js"),
            ("__REBUILT_DEDUP__", "rebuilt_dedup.js"),
            ("__DENSITY_POLICY__", "density_policy.js"),
        ):
            template = template.replace(
                marker, Path(__file__).with_name(filename).read_text()
            )
        before, after = template.split("__DATA__")
        encoder = json.JSONEncoder(
            ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        with temporary.open("w", encoding="utf8") as stream:
            stream.write(before)
            for chunk in encoder.iterencode(payload):
                stream.write(chunk.replace("<", "\\u003c"))
            stream.write(after)
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
    p.add_argument("--dedup")
    p.add_argument("--overlap")
    a = p.parse_args()
    print(
        json.dumps(
            export(a.source, a.coverage, a.output, a.dedup, overlap=a.overlap),
            ensure_ascii=False,
        )
    )
