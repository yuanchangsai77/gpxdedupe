"""Opt-in local engine probes. HTTP success is never reconstruction approval.

Adapters intentionally refuse edge constraints they cannot faithfully enforce.
"""

import json
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


class UnsupportedConstraint(ValueError):
    pass


class EngineAdapter:
    def __init__(self, url, graph_version, timeout=20):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError("local_engine_required")
        self.url = url.rstrip("/")
        self.graph_version = graph_version
        self.timeout = timeout
        self.route_request_count = 0
        self.match_request_count = 0
        # Local GPS requests must reach the local engine, not an environment proxy.
        self._opener = build_opener(ProxyHandler({}))

    def request(self, path, payload):
        request = Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("response_limit")
        return json.loads(raw)

    def route(self, points, mode="foot", mandatory_edges=(), forbidden_edges=()):
        if mandatory_edges or forbidden_edges:
            raise UnsupportedConstraint("native_edge_constraints_unavailable")
        self.route_request_count += 1
        return self._route(points, mode)


def collapse_continuous_edges(edges):
    # Via points can split one directed edge into adjacent path-detail records.
    # A return trip necessarily includes the reverse edge, which remains intact.
    result = []
    for edge in edges:
        if not result or result[-1] != edge:
            result.append(edge)
    return result


class GraphHopperAdapter(EngineAdapter):
    def nearest(self, point):
        """Diagnostic ALL_EDGES snap; a subsequent foot route must validate access."""
        from urllib.parse import urlencode

        with self._opener.open(
            self.url + "/nearest?" + urlencode({"point": f"{point[1]},{point[0]}"}),
            timeout=self.timeout,
        ) as response:
            return json.load(response)

    def wait_ready(self, timeout=20):
        import time
        from urllib.error import HTTPError, URLError

        deadline = time.monotonic() + timeout
        while True:
            try:
                with self._opener.open(
                    self.url + "/health", timeout=min(2, self.timeout)
                ) as response:
                    if response.status == 200:
                        return
            except (HTTPError, URLError, TimeoutError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise URLError("local routing engine is not ready")
            time.sleep(0.25)

    def _route(self, points, mode):
        if mode not in ("foot", "hike", "bike"):
            raise ValueError("unsupported_mode")
        data = self.request(
            "/route",
            {
                "points": points,
                "profile": mode,
                "points_encoded": False,
                "instructions": False,
                "details": ["edge_key", "osm_way_id"],
            },
        )
        paths = data.get("paths", [])
        if not paths:
            raise ValueError("no_path")
        return {
            "geometry": paths[0]["points"],
            "road_graph_version": self.graph_version,
            "status": "unvalidated",
            "engine": "graphhopper",
            "edge_details": paths[0].get("details", {}).get("edge_key", []),
            "osm_way_ids": [
                row[2] for row in paths[0].get("details", {}).get("osm_way_id", [])
            ],
            "directed_edges": collapse_continuous_edges(
                [row[2] for row in paths[0].get("details", {}).get("edge_key", [])]
            ),
        }
