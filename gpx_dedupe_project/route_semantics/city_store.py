"""Isolated city experiment store: immutable evidence, ordered controls, revisions."""

import hashlib
import json
import os
import sqlite3
import zlib
from pathlib import Path


def pack(value):
    return zlib.compress(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode(),
        3,
    )


def unpack(value):
    return json.loads(zlib.decompress(value)) if value else None


class CityStore:
    def __init__(self, path, initialize=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = (
            sqlite3.connect(self.path, timeout=60)
            if initialize
            else sqlite3.connect(
                "file:" + str(self.path.resolve()) + "?mode=ro", uri=True, timeout=10
            )
        )
        self.db.row_factory = sqlite3.Row
        if not initialize:
            return
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,path TEXT,size INTEGER,mtime_ns INTEGER,offset INTEGER DEFAULT 0,complete INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY,source_id TEXT REFERENCES sources(id),source_offset INTEGER,content_sha256 TEXT,activity_codes TEXT,parts BLOB,display BLOB,point_count INTEGER,distance_m REAL,min_lon REAL,max_lon REAL,min_lat REAL,max_lat REAL,state TEXT DEFAULT 'queued',error TEXT,definition BLOB,rebuilt BLOB,rebuilt_display BLOB,graph_version TEXT,algorithm_version TEXT,updated REAL,UNIQUE(source_id,source_offset));
        CREATE VIRTUAL TABLE IF NOT EXISTS route_bounds USING rtree(id,min_lon,max_lon,min_lat,max_lat);
        CREATE INDEX IF NOT EXISTS route_state ON routes(state,id);
        CREATE TABLE IF NOT EXISTS results(route_id INTEGER REFERENCES routes(id),graph_version TEXT,algorithm_version TEXT,state TEXT,payload BLOB,elapsed_s REAL,created REAL,PRIMARY KEY(route_id,graph_version,algorithm_version));
        CREATE TABLE IF NOT EXISTS anchors(id TEXT PRIMARY KEY,kind TEXT,name TEXT,lon REAL,lat REAL,source TEXT,review_status TEXT,payload BLOB);
        CREATE VIRTUAL TABLE IF NOT EXISTS anchor_bounds USING rtree(id,min_lon,max_lon,min_lat,max_lat);
        CREATE INDEX IF NOT EXISTS anchor_source ON anchors(source);
        CREATE TABLE IF NOT EXISTS route_anchors(route_id INTEGER REFERENCES routes(id),graph_version TEXT,algorithm_version TEXT,part INTEGER,sequence INTEGER,anchor_id TEXT REFERENCES anchors(id),lon REAL,lat REAL,relation TEXT,PRIMARY KEY(route_id,graph_version,algorithm_version,part,sequence));
        CREATE INDEX IF NOT EXISTS anchor_routes ON route_anchors(anchor_id,route_id);
        CREATE TABLE IF NOT EXISTS audit(at REAL,event TEXT,route_id INTEGER,details TEXT);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        """)

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES(?,?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put_anchor(
        self,
        identifier,
        coordinate,
        kind="anonymous_control",
        name=None,
        source="trace_evidence",
        payload=None,
    ):
        self.db.execute(
            "INSERT OR IGNORE INTO anchors VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                kind,
                name,
                *coordinate,
                source,
                "unreviewed",
                pack(payload or {}),
            ),
        )
        row = self.db.execute(
            "SELECT rowid FROM anchors WHERE id=?", (identifier,)
        ).fetchone()
        self.db.execute(
            "INSERT OR REPLACE INTO anchor_bounds VALUES(?,?,?,?,?)",
            (row[0], coordinate[0], coordinate[0], coordinate[1], coordinate[1]),
        )

    def link_controls(self, route_id, graph, algorithm, parts):
        self.db.execute(
            "DELETE FROM route_anchors WHERE route_id=? AND graph_version=? AND algorithm_version=?",
            (route_id, graph, algorithm),
        )
        for part, product in enumerate(parts):
            for seq, control in enumerate(product.get("controls", [])):
                coordinate = control["coordinate"]
                lon, lat = coordinate
                # Shared entities are references with explicit proximity evidence;
                # they do not become mandatory planning anchors merely by distance.
                candidates = self.db.execute(
                    "SELECT a.* FROM anchor_bounds b CROSS JOIN anchors a ON a.rowid=b.id WHERE a.source=? AND b.max_lon>=? AND b.min_lon<=? AND b.max_lat>=? AND b.min_lat<=?",
                    (
                        "OpenStreetMap",
                        lon - 0.0003,
                        lon + 0.0003,
                        lat - 0.0003,
                        lat + 0.0003,
                    ),
                ).fetchall()
                from .geometry import distance

                candidates = sorted(
                    candidates, key=lambda r: distance(coordinate, [r["lon"], r["lat"]])
                )
                entity = (
                    candidates[0]
                    if candidates
                    and distance(
                        coordinate, [candidates[0]["lon"], candidates[0]["lat"]]
                    )
                    <= 20
                    else None
                )
                identifier = (
                    entity["id"]
                    if entity
                    else "private:"
                    + hashlib.sha256(
                        json.dumps([route_id, part, seq, coordinate]).encode()
                    ).hexdigest()[:24]
                )
                if not entity:
                    self.put_anchor(identifier, coordinate)
                self.db.execute(
                    "INSERT OR REPLACE INTO route_anchors VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        route_id,
                        graph,
                        algorithm,
                        part,
                        seq,
                        identifier,
                        lon,
                        lat,
                        "nearby_entity_candidate"
                        if entity
                        else "route_private_control",
                    ),
                )

    def summary(self):
        counts = dict(
            self.db.execute(
                "SELECT state,count(*) FROM routes GROUP BY state"
            ).fetchall()
        )
        run = self.meta("run")
        if run and run.get("state") == "running" and run.get("pid"):
            try:
                os.kill(run["pid"], 0)
            except ProcessLookupError:
                run = {**run, "state": "interrupted"}
        return {
            "total": sum(counts.values()),
            "states": counts,
            "sources": [
                dict(r)
                for r in self.db.execute("SELECT id,size,offset,complete FROM sources")
            ],
            "run": run,
            "samples": self.meta("samples") or [],
            "coverage": self.meta("graph_coverage"),
            "extent": list(
                self.db.execute(
                    "SELECT min(min_lon),min(min_lat),max(max_lon),max(max_lat) FROM route_bounds"
                ).fetchone()
            ),
            "anchor_count": self.db.execute("SELECT count(*) FROM anchors").fetchone()[
                0
            ],
            "shared_entity_count": self.db.execute(
                "SELECT count(*) FROM anchors WHERE source='OpenStreetMap'"
            ).fetchone()[0],
        }
