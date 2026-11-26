"""Loopback-only experimental city map. Existing application APIs are untouched."""

import argparse
import collections
import concurrent.futures
import hashlib
import io
import json
import math
import os
import resource
import sqlite3
import struct
import tempfile
import zlib

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageDraw

from .city_store import CityStore, unpack


def projected(points, z):
    p = np.asarray(points, dtype=np.float64)
    lon = p[:, 0].copy()
    lat = p[:, 1].copy()
    x = lon - 105
    y = lat - 35
    a = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * np.sqrt(np.abs(x))
    a += (20 * np.sin(6 * x * np.pi) + 20 * np.sin(2 * x * np.pi)) * 2 / 3
    a += (20 * np.sin(y * np.pi) + 40 * np.sin(y * np.pi / 3)) * 2 / 3
    a += (160 * np.sin(y * np.pi / 12) + 320 * np.sin(y * np.pi / 30)) * 2 / 3
    b = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * np.sqrt(np.abs(x))
    b += (20 * np.sin(6 * x * np.pi) + 20 * np.sin(2 * x * np.pi)) * 2 / 3
    b += (20 * np.sin(x * np.pi) + 40 * np.sin(x * np.pi / 3)) * 2 / 3
    b += (150 * np.sin(x * np.pi / 12) + 300 * np.sin(x * np.pi / 30)) * 2 / 3
    rad = np.radians(lat)
    magic = 1 - 0.00669342162296594323 * np.sin(rad) ** 2
    valid = (lon > 72.004) & (lon < 137.8347) & (lat > 0.8293) & (lat < 55.8271)
    lat += np.where(
        valid,
        a
        * 180
        / ((6378245 * (1 - 0.00669342162296594323)) / (magic * np.sqrt(magic)) * np.pi),
        0,
    )
    lon += np.where(
        valid, b * 180 / (6378245 / np.sqrt(magic) * np.cos(rad) * np.pi), 0
    )
    scale = 256 * 2**z
    return np.column_stack(
        (
            (lon + 180) / 360 * scale,
            (1 - np.arcsinh(np.tan(np.radians(np.clip(lat, -85, 85)))) / np.pi)
            / 2
            * scale,
        )
    )


def source_cache_path(database, store):
    snapshot = [
        list(r) for r in store.db.execute("SELECT id,size FROM sources ORDER BY id")
    ]
    total = store.db.execute("SELECT count(*) FROM routes").fetchone()[0]
    signature = hashlib.sha256(
        json.dumps([snapshot, total, "display-80m-world18-v2"]).encode()
    ).hexdigest()[:20]
    namespace = hashlib.sha256(str(Path(database).resolve()).encode()).hexdigest()[:16]
    root = Path(tempfile.gettempdir()) / ("gpxdedupe-city-map-" + namespace)
    root.mkdir(parents=True, exist_ok=True)
    return root / ("source-map-" + signature + ".sqlite3"), total


def prepare_source_cache(database):
    source = CityStore(database, initialize=False)
    path, total = source_cache_path(database, source)
    previous = Path(database).parent / path.name
    if not path.exists() and previous.exists():
        # The Windows-mounted source DB stays untouched. Copy only its completed,
        # reproducible display cache onto the much faster Linux filesystem.
        old = sqlite3.connect("file:" + str(previous.resolve()) + "?mode=ro", uri=True)
        ready = old.execute(
            "SELECT value FROM readiness WHERE key='complete'"
        ).fetchone()
        if ready and ready[0] == total:
            temporary = path.with_suffix(".copying.sqlite3")
            copied = sqlite3.connect(temporary)
            old.backup(copied)
            copied.close()
            temporary.replace(path)
        old.close()
    target = sqlite3.connect(path, timeout=30)
    target.executescript(
        "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS geometry(route_id INTEGER PRIMARY KEY,original BLOB,world BLOB); CREATE TABLE IF NOT EXISTS readiness(key TEXT PRIMARY KEY,value INTEGER);"
    )
    ready = target.execute(
        "SELECT value FROM readiness WHERE key='complete'"
    ).fetchone()
    if ready and ready[0] == total:
        target.close()
        source.db.close()
        return
    last = 0
    while True:
        rows = source.db.execute(
            "SELECT id,display FROM routes WHERE id>? ORDER BY id LIMIT 200", (last,)
        ).fetchall()
        if not rows:
            break
        with target:
            target.executemany(
                "INSERT OR IGNORE INTO geometry VALUES(?,?,?)",
                [
                    (r["id"], r["display"], encode_world(unpack(r["display"])))
                    for r in rows
                ],
            )
        last = rows[-1]["id"]
    with target:
        target.execute(
            "INSERT OR REPLACE INTO readiness VALUES('complete',?)", (total,)
        )
    target.close()
    source.db.close()
    print("SOURCE_MAP_CACHE_READY", total, flush=True)


def encode_world(parts):
    arrays = [projected(p, 18).astype("<f8") for p in (parts or []) if len(p) > 1]
    header = struct.pack("<I", len(arrays))
    return zlib.compress(
        header + b"".join(struct.pack("<I", len(a)) + a.tobytes() for a in arrays), 1
    )


def decode_world(blob):
    raw = zlib.decompress(blob)
    count = struct.unpack_from("<I", raw)[0]
    offset = 4
    result = []
    for _ in range(count):
        size = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        result.append(
            np.frombuffer(raw, dtype="<f8", count=size * 2, offset=offset).reshape(
                -1, 2
            )
        )
        offset += size * 16
    return result


def pick_routes(database, x, y, z, review=True, radius=10):
    if not 0 <= z <= 18 or not all(math.isfinite(v) for v in (x, y)):
        raise ValueError("invalid_map_position")
    scale = 256 * 2**z
    if not 0 <= x < scale or not 0 <= y < scale:
        raise ValueError("invalid_map_position")
    lon = x / scale * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    delta = radius / scale * 360 + 0.015  # Conservative WGS/GCJ recall envelope.
    store = CityStore(database, initialize=False)
    states = "('candidate','review')" if review else "('candidate')"
    column = "rebuilt" if z >= 14 else "rebuilt_display"
    found = []
    try:
        for row in store.db.execute(
            f"SELECT r.id,r.state,r.distance_m,r.{column} AS geometry FROM route_bounds b CROSS JOIN routes r ON r.id=b.id WHERE b.max_lon>=? AND b.min_lon<=? AND b.max_lat>=? AND b.min_lat<=? AND r.state IN {states}",
            (lon - delta, lon + delta, lat - delta, lat + delta),
        ):
            best = math.inf
            for part in unpack(row["geometry"]) or []:
                if len(part) < 2:
                    continue
                p = projected(part, z)
                a, v = p[:-1], p[1:] - p[:-1]
                norm = np.sum(v * v, axis=1)
                t = np.clip(
                    np.divide(
                        np.sum((np.array([x, y]) - a) * v, axis=1),
                        norm,
                        out=np.zeros_like(norm),
                        where=norm > 0,
                    ),
                    0,
                    1,
                )
                best = min(
                    best,
                    float(np.min(np.linalg.norm(a + t[:, None] * v - [x, y], axis=1))),
                )
            if best <= radius:
                found.append(
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "distance_m": row["distance_m"],
                        "pixel_distance": round(best, 3),
                    }
                )
        found.sort(key=lambda r: (r["pixel_distance"], r["id"]))
        return {"routes": found[:12], "total_matches": len(found), "radius_px": radius}
    finally:
        store.db.close()


def tile(database, layer, z, x, y, matched=False, review=True):
    if not 0 <= z <= 18 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise ValueError("invalid_tile")
    west = x / 2**z * 360 - 180
    east = (x + 1) / 2**z * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2**z))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / 2**z))))
    store = CityStore(database, initialize=False)
    column = (
        ("parts" if z >= 14 else "display")
        if layer == "original"
        else ("rebuilt" if z >= 14 else "rebuilt_display")
    )
    states = (
        " AND r.state IN ('candidate','review')"
        if review
        else " AND r.state='candidate'"
    )
    condition = states if layer == "rebuilt" or matched else ""
    query = f"SELECT r.{column},r.state FROM route_bounds b CROSS JOIN routes r ON r.id=b.id WHERE b.max_lon>=? AND b.min_lon<=? AND b.max_lat>=? AND b.min_lat<=?{condition}"
    world_cached = False
    if layer == "original" and z < 14:
        path, total = source_cache_path(database, store)
        if path.exists():
            try:
                store.db.execute(
                    "ATTACH DATABASE ? AS lod",
                    ("file:" + str(path.resolve()) + "?mode=ro",),
                )
                ready = store.db.execute(
                    "SELECT value FROM lod.readiness WHERE key='complete'"
                ).fetchone()
                if ready and ready[0] == total:
                    join = (
                        " CROSS JOIN routes r INDEXED BY route_state ON r.id=b.id"
                        if matched
                        else ""
                    )
                    state_column = "r.state" if matched else "'original'"
                    query = (
                        f"SELECT g.world,{state_column} AS state FROM route_bounds b CROSS JOIN lod.geometry g ON g.route_id=b.id{join} WHERE b.max_lon>=? AND b.min_lon<=? AND b.max_lat>=? AND b.min_lat<=?"
                        + (states if matched else "")
                    )
                    world_cached = True
            except sqlite3.Error:
                pass  # A not-yet-ready derived cache never hides source routes.

    masks = [
        Image.new("RGB", (256, 256), "black"),
        Image.new("RGB", (256, 256), "black"),
    ]
    draws = [ImageDraw.Draw(m, "RGBA") for m in masks]
    count = 0
    for row in store.db.execute(
        query, (west - 0.015, east + 0.015, south - 0.015, north + 0.015)
    ):
        count += 1
        parts = decode_world(row[0]) if world_cached else unpack(row[0])
        if not parts:
            continue
        draw = draws[1 if layer == "rebuilt" and row["state"] == "review" else 0]
        for points in parts:
            if len(points) < 2:
                continue
            p = (points * 2.0 ** (z - 18) if world_cached else projected(points, z)) - [
                x * 256,
                y * 256,
            ]
            draw.line(
                [tuple(v) for v in p],
                fill=(255, 255, 255, 35),
                width=2 if z >= 14 else 1,
            )
    result = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    for mask, color in zip(
        masks,
        [(225, 102, 58) if layer == "original" else (22, 104, 200), (137, 72, 181)],
    ):
        alpha = mask.getchannel("R")
        colored = Image.new("RGBA", (256, 256), (*color, 255))
        colored.putalpha(alpha)
        result = Image.alpha_composite(result, colored)
    out = io.BytesIO()
    result.save(out, format="PNG")
    store.db.close()
    return out.getvalue(), count


def serve(database, port=8766):
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    disk_cache = Path(database).parent / "map-cache"
    disk_cache.mkdir(exist_ok=True)
    cache = collections.OrderedDict()
    lock = threading.Lock()
    render_slots = threading.Semaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content="application/json", status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Cache-Control", (headers or {}).get("Cache-Control", "no-cache")
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                if k != "Cache-Control":
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, value, status=200):
            self.send(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode(),
                status=status,
            )

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    self.send(
                        Path(__file__).with_name("city_view.html").read_bytes(),
                        "text/html; charset=utf-8",
                    )
                    return
                if u.path == "/reconstruction.html":
                    self.send(
                        Path(__file__)
                        .parent.parent.joinpath("web", "reconstruction.html")
                        .read_bytes(),
                        "text/html; charset=utf-8",
                    )
                    return
                if u.path == "/motion.js":
                    self.send(
                        Path(__file__).with_name("route_motion.js").read_bytes(),
                        "application/javascript",
                    )
                    return
                if u.path == "/status":
                    store = CityStore(database, initialize=False)
                    self.json(store.summary())
                    store.db.close()
                    return
                if u.path == "/tiles":
                    layer = q.get("layer", ["original"])[0]
                    if layer not in ("original", "rebuilt"):
                        raise ValueError("invalid_layer")
                    z, x, y = (int(q[k][0]) for k in ("z", "x", "y"))
                    matched = q.get("matched", ["0"])[0] == "1"
                    review = q.get("review", ["1"])[0] == "1"
                    epoch = q.get("v", ["0"])[0][:128]
                    key = (layer, z, x, y, matched, review, epoch)
                    with lock:
                        cached = cache.get(key)
                    if cached is None:
                        cache_file = disk_cache / (
                            hashlib.sha256(
                                json.dumps(["city-png-2", *key]).encode()
                            ).hexdigest()
                            + ".png"
                        )
                        if cache_file.exists():
                            cached = (cache_file.read_bytes(), -1)
                        else:
                            with render_slots:
                                cached = tile(database, layer, z, x, y, matched, review)
                            temporary = cache_file.with_suffix(
                                "." + str(threading.get_ident()) + ".tmp"
                            )
                            temporary.write_bytes(cached[0])
                            temporary.replace(cache_file)
                        with lock:
                            cache[key] = cached
                            while len(cache) > 128:
                                cache.popitem(last=False)
                    self.send(
                        cached[0],
                        "image/png",
                        headers={
                            "X-Routes-Considered": str(cached[1]),
                            "Cache-Control": "private, max-age=86400, immutable"
                            if epoch != "0"
                            else "no-cache",
                        },
                    )
                    return
                if u.path == "/pick":
                    self.json(
                        pick_routes(
                            database,
                            float(q["x"][0]),
                            float(q["y"][0]),
                            int(q["z"][0]),
                            q.get("review", ["1"])[0] == "1",
                        )
                    )
                    return
                if u.path == "/route":
                    store = CityStore(database, initialize=False)
                    identifier = int(q["id"][0])
                    r = store.db.execute(
                        "SELECT * FROM routes WHERE id=?", (identifier,)
                    ).fetchone()
                    if not r:
                        self.json({"error": "route_not_found"}, 404)
                        return
                    payload = unpack(r["definition"])
                    self.json(
                        {
                            "id": identifier,
                            "state": r["state"],
                            "original": unpack(r["parts"]),
                            "rebuilt": (unpack(r["rebuilt"]) or [])
                            if r["state"] in ("candidate", "review")
                            else [],
                            "definition": payload,
                            "nearby_entities": [
                                dict(v)
                                for v in store.db.execute(
                                    "SELECT DISTINCT a.id,a.name,a.kind FROM route_anchors ra JOIN anchors a ON ra.anchor_id=a.id WHERE ra.route_id=? AND a.source=? AND a.name IS NOT NULL LIMIT 30",
                                    (identifier, "OpenStreetMap"),
                                )
                            ],
                            "controls": [
                                dict(v)
                                for v in store.db.execute(
                                    "SELECT part,sequence,anchor_id,lon,lat,relation FROM route_anchors WHERE route_id=? AND graph_version=? AND algorithm_version=? ORDER BY part,sequence",
                                    (
                                        identifier,
                                        r["graph_version"],
                                        r["algorithm_version"],
                                    ),
                                )
                            ],
                        }
                    )
                    store.db.close()
                    return
                if u.path == "/routes":
                    store = CityStore(database, initialize=False)
                    state = q.get("state", ["all"])[0]
                    where = "" if state == "all" else "WHERE state=?"
                    args = [] if state == "all" else [state]
                    rows = [
                        dict(r)
                        for r in store.db.execute(
                            f"SELECT id,state,distance_m,point_count,error FROM routes {where} ORDER BY id LIMIT 100",
                            args,
                        )
                    ]
                    self.json(
                        {
                            "routes": rows,
                            "list_limit": 100,
                            "map_has_no_route_count_limit": True,
                        }
                    )
                    store.db.close()
                    return
                self.json({"error": "not_found"}, 404)
            except (ValueError, KeyError) as exc:
                self.json({"error": str(exc)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001 -- HTTP error boundary; source DB is read-only.
                import traceback

                traceback.print_exc()
                self.json({"error": type(exc).__name__ + ":" + str(exc)[:180]}, 500)

    class BoundedServer(ThreadingHTTPServer):
        request_queue_size = 64

        def __init__(self, *args):
            self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
            self.slots = threading.BoundedSemaphore(32)
            super().__init__(*args)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return

            def handle():
                try:
                    self.process_request_thread(request, address)
                finally:
                    self.slots.release()

            self.pool.submit(handle)

    threading.stack_size(512 * 1024)
    threading.Thread(target=prepare_source_cache, args=(database,), daemon=True).start()
    print("CITY_VIEW http://localhost:" + str(port), flush=True)
    BoundedServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--port", type=int, default=8766)
    a = p.parse_args()
    serve(a.database, a.port)
