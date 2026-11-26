"""Bounded endpoint recovery; observation endpoints remain immutable."""

import io
import json
from itertools import pairwise, product
from urllib.error import HTTPError

from .geometry import distance


def endpoint_candidates(points, reverse=False, maximum_m=60):
    points = list(reversed(points)) if reverse else points
    candidates = [
        {
            "coordinate": points[0],
            "source_index": len(points) - 1 if reverse else 0,
            "omitted_observed_m": 0,
        }
    ]
    travelled = 0
    for index, (a, b) in enumerate(pairwise(points), 1):
        travelled += distance(a, b)
        if travelled > maximum_m or index > 3:
            break
        candidates.append(
            {
                "coordinate": b,
                "source_index": len(points) - 1 - index if reverse else index,
                "omitted_observed_m": travelled,
            }
        )
    return candidates


class EndpointRecovery:
    def __init__(self, base, source, maximum_calls=20):
        self.base = base
        self.source = source
        self.graph_version = base.graph_version
        self.initial = base.route_request_count
        self.maximum_calls = maximum_calls
        self.adjustments = []

    def _route(self, points):
        if self.base.route_request_count - self.initial >= self.maximum_calls:
            raise ValueError("endpoint_recovery_request_budget")
        return self.base.route(points, "foot")

    def route(self, points, mode):
        if mode != "foot":
            raise ValueError("foot_only")
        try:
            return self._route(points)
        except HTTPError as error:
            data = error.read(8192)
            try:
                missing = {
                    h["point_index"]
                    for h in json.loads(data).get("hints", [])
                    if "PointNotFound" in h.get("details", "")
                }
            except (ValueError, KeyError):
                missing = set()
            if error.code != 400 or not missing or missing - {0, len(points) - 1}:
                raise HTTPError(
                    error.url, error.code, error.msg, error.headers, io.BytesIO(data)
                ) from None
            starts = (
                endpoint_candidates(self.source)
                if 0 in missing
                else endpoint_candidates(self.source)[:1]
            )
            ends = (
                endpoint_candidates(self.source, True)
                if len(points) - 1 in missing
                else endpoint_candidates(self.source, True)[:1]
            )
            if hasattr(self.base, "nearest"):
                for choices, observed, index, missing_index in (
                    (starts, self.source[0], 0, 0),
                    (ends, self.source[-1], len(self.source) - 1, len(points) - 1),
                ):
                    if missing_index not in missing:
                        continue
                    try:
                        snap = self.base.nearest(observed)
                    except HTTPError:
                        continue
                    if 0 < snap.get("distance", 1e9) <= 150:
                        choices.append(
                            {
                                "coordinate": snap["coordinates"][:2],
                                "source_index": index,
                                "omitted_observed_m": 0,
                                "snap_distance_m": snap["distance"],
                                "foot_access_validation": "subsequent_foot_route_required",
                            }
                        )
            choices = sorted(
                product(starts, ends),
                key=lambda p: sum(
                    c["omitted_observed_m"] + c.get("snap_distance_m", 0) for c in p
                ),
            )
            for start, end in choices:
                if (
                    sum(
                        c["omitted_observed_m"] + c.get("snap_distance_m", 0)
                        for c in (start, end)
                    )
                    == 0
                ):
                    continue
                adjusted = [start["coordinate"], *points[1:-1], end["coordinate"]]
                try:
                    result = self._route(adjusted)
                except HTTPError as retry:
                    if retry.code != 400:
                        raise
                    continue
                self.adjustments.append(
                    {
                        "observed_start": self.source[0],
                        "observed_end": self.source[-1],
                        "planning_start": start,
                        "planning_end": end,
                        "no_observed_connector": True,
                    }
                )
                return {
                    **result,
                    "planning_controls": adjusted,
                    "endpoint_adjustments": self.adjustments[-1:],
                }
            raise HTTPError(
                error.url, error.code, error.msg, error.headers, io.BytesIO(data)
            ) from None
