"""Quality-gated coverage selection and whole-route geometry decisions.

All thresholds are explicit experimental policy, not calibrated accuracy claims.
No routing requests, source writes, or semantic deduplication.
"""

import argparse
import heapq
import json
import sqlite3
import time

from .city_store import CityStore


def quality(length, gap):
    if length <= 0:
        return "review", "没有可统计的连续轨迹"
    if gap > 0:
        return "review", "存在超过采样间隔上限的观测间隙，待复核"
    return "eligible", "坐标与观测间隙基础检查通过，未验证通行"


def geometry_choice(raw_quality, state, has_geometry):
    if raw_quality != "eligible":
        return "none", "原始证据需复核，不因规划返回结果自动放行"
    if state == "candidate" and has_geometry:
        return "rebuilt", "采用已有质量候选，不代表导航认证"
    if state == "review":
        return "original", "重建结果待复核，暂展示完整原始轨迹"
    return "original", "没有通过质量检查的重建结果，保留完整原始轨迹"


def run(source, output, size=100, sparse_count=2, target=0.95):
    if not 0 < target <= 1 or sparse_count < 1 or size <= 0:
        raise ValueError("invalid selection policy")
    from pathlib import Path

    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("output must be separate from source")
    began = time.monotonic()
    store = CityStore(source, initialize=False)
    db = sqlite3.connect(output)
    try:
        ready = db.execute(
            "SELECT value FROM metadata WHERE key='coverage_ready'"
        ).fetchone()
        if ready and ready[0] != "true":
            raise ValueError("coverage index incomplete: resume build first")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS selection_runs(id INTEGER PRIMARY KEY,created REAL,policy TEXT,report TEXT);
        CREATE TABLE IF NOT EXISTS decisions(run_id INTEGER,route_id INTEGER,raw_quality TEXT,geometry_kind TEXT,selected INTEGER,rank INTEGER,novel_length_m REAL,sparse_length_m REAL,reason TEXT,PRIMARY KEY(run_id,route_id));
        """)
        policy = {
            "version": "coverage-selection-2",
            "size": size,
            "sparse_count": sparse_count,
            "target": target,
            "sparse_policy": "protect_all_eligible_routes_with_sparse_cells",
            "quality": "coordinate_and_gap_only",
        }
        cursor = db.execute(
            "INSERT INTO selection_runs(created,policy) VALUES(?,?)",
            (time.time(), json.dumps(policy)),
        )
        run_id = cursor.lastrowid
        db.commit()
        records = {}
        routes = {}
        counts = {
            (x, y): n
            for x, y, n in db.execute(
                "SELECT x,y,route_count FROM cells WHERE size=?", (size,)
            )
        }
        if not counts:
            raise ValueError("requested grid scale is absent")
        for rid, length, gap, sha in db.execute(
            "SELECT id,length_m,gap_m,sha FROM routes ORDER BY id"
        ):
            row = store.db.execute(
                "SELECT state,rebuilt IS NOT NULL AS has_geometry,content_sha256 FROM routes WHERE id=?",
                (rid,),
            ).fetchone()
            if row is None:
                raise ValueError(f"missing source route {rid}")
            if row["content_sha256"] != sha:
                raise ValueError(f"source route changed: {rid}")
            q, qreason = quality(length, gap)
            kind, reason = geometry_choice(q, row["state"], row["has_geometry"])
            records[rid] = {
                "quality": q,
                "kind": kind,
                "reason": qreason + "；" + reason,
                "sparse": 0,
                "rank": None,
                "novel": 0,
            }
        for rid, x, y, length in db.execute(
            "SELECT route_id,x,y,length_m FROM contributions WHERE size=?", (size,)
        ):
            if records[rid]["quality"] == "eligible":
                routes.setdefault(rid, {})[(x, y)] = length
                if counts[(x, y)] <= sparse_count:
                    records[rid]["sparse"] += length
        universe = set()
        inverted = {}
        scores = {}
        for rid, cells in routes.items():
            scores[rid] = sum(min(v, size) for v in cells.values())
            for cell, value in cells.items():
                universe.add(cell)
                inverted.setdefault(cell, []).append((rid, min(value, size)))
        covered = set()
        selected = set()

        def choose(rid, reason):
            record = records[rid]
            record["rank"] = len(selected) + 1
            record["novel"] = sum(v for k, v in routes[rid].items() if k not in covered)
            record["reason"] += "；" + reason
            selected.add(rid)
            for cell in routes[rid]:
                if cell in covered:
                    continue
                covered.add(cell)
                for other, value in inverted[cell]:
                    scores[other] = max(0, scores[other] - value)

        # Accepted rebuilt candidates are mandatory display members.
        for rid in sorted(routes):
            if records[rid]["kind"] == "rebuilt":
                choose(rid, "已有通过候选标准的重建结果，固定保留")
        # Protect sparse coverage first. No direction/loop/out-and-back gate.
        for rid in sorted(routes, key=lambda rid: (-records[rid]["sparse"], rid)):
            if rid not in selected and records[rid]["sparse"] > 0:
                choose(rid, "包含稀疏网格，优先保留")
        heap = [(-scores[rid], rid) for rid in routes if rid not in selected]
        heapq.heapify(heap)
        while heap and len(covered) < target * len(universe):
            negative, rid = heapq.heappop(heap)
            if rid in selected:
                continue
            score = scores[rid]
            if abs(-negative - score) > 1e-7:
                heapq.heappush(heap, (-score, rid))
                continue
            if score <= 1e-7:
                break
            choose(rid, "补充已选集合尚未覆盖的网格")
        rows = []
        for rid, r in records.items():
            if rid not in selected:
                r["reason"] += (
                    "；待复核"
                    if r["quality"] != "eligible"
                    else "；降低默认展示优先级，原始记录仍保留"
                )
            rows.append(
                (
                    run_id,
                    rid,
                    r["quality"],
                    r["kind"],
                    int(rid in selected),
                    r["rank"],
                    r["novel"],
                    r["sparse"],
                    r["reason"],
                )
            )
        report = {
            "run_id": run_id,
            "total": len(records),
            "selected": len(selected),
            "eligible": len(routes),
            "covered_cells": len(covered),
            "eligible_cells": len(universe),
            "selected_original": sum(
                records[r]["kind"] == "original" for r in selected
            ),
            "selected_rebuilt": sum(records[r]["kind"] == "rebuilt" for r in selected),
            "elapsed_s": time.monotonic() - began,
            "policy": policy,
        }
        with db:
            db.executemany("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)", rows)
            db.execute(
                "UPDATE selection_runs SET report=? WHERE id=?",
                (json.dumps(report), run_id),
            )
        return report
    finally:
        db.close()
        store.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--coverage", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.coverage), ensure_ascii=False))
