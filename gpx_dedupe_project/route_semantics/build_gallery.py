"""Generate a standalone route selector including rejected reconstruction cases."""

import argparse
import html
import json
import math
from pathlib import Path


def static_map(observed, rebuilt, candidate=(), partials=(), display_segments=()):
    """Inline SVG remains visible when a file-preview host blocks scripts."""
    latitude = math.radians(observed[0][1])

    def project(point):
        return (
            math.radians(point[0]) * math.cos(latitude) * 6371000,
            math.radians(point[1]) * 6371000,
        )

    original = [project(p) for p in observed]
    reconstructed = [project(p) for p in rebuilt]
    candidate_points = [project(p) for p in candidate]
    partial_points = [[project(p) for p in segment] for segment in partials]
    combined = (
        original
        + reconstructed
        + candidate_points
        + [p for segment in partial_points for p in segment]
    )
    lo = [min(p[i] for p in combined) for i in (0, 1)]
    hi = [max(p[i] for p in combined) for i in (0, 1)]
    scale = min(850 / max(hi[0] - lo[0], 1), 660 / max(hi[1] - lo[1], 1))

    def screen(p):
        return (
            500 + (p[0] - (lo[0] + hi[0]) / 2) * scale,
            400 - (p[1] - (lo[1] + hi[1]) / 2) * scale,
        )

    def polyline(points, color, width, dash=""):
        coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in map(screen, points))
        return f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" stroke-dasharray="{dash}" stroke-linejoin="round"/>'

    paths = polyline(original, "#e16d42", 5, "8 5") + polyline(
        reconstructed, "#156cca", 3
    )
    if candidate_points:
        paths += polyline(candidate_points, "#8a4fbf", 3, "5 5")
    for segment in partial_points:
        paths += polyline(segment, "#8a4fbf", 3, "5 5")
    if display_segments:
        paths = ""
        for segment in display_segments:
            points = [project(p) for p in segment["geometry"]["coordinates"]]
            paths += polyline(
                points,
                "#156cca" if segment["kind"] == "road" else "#bd8526",
                4,
                "" if segment["kind"] == "road" else "7 4",
            )
    for label, p, color in [
        ("S", original[0], "#277757"),
        ("T", original[-1], "#303d47"),
    ]:
        x, y = screen(p)
        paths += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="13" fill="{color}"/><text x="{x:.2f}" y="{y + 4:.2f}" fill="white" font-size="13" text-anchor="middle">{label}</text>'
    return (
        '<svg id="fallback-map" viewBox="0 0 1000 800" role="img" aria-label="原始 GPS 与重建路线静态对照">'
        + paths
        + "</svg>"
    )


def build(source, output):
    report = json.loads(Path(source).read_text(encoding="utf-8"))
    routes = report["routes"]
    if not routes:
        raise ValueError("no_routes")
    template = (
        Path(__file__).with_name("comparison.template.html").read_text(encoding="utf-8")
    )
    template = template.replace(
        "__MOTION_SCRIPT__",
        Path(__file__).with_name("route_motion.js").read_text(encoding="utf-8"),
    )
    payload = json.dumps(
        {"routes": routes}, ensure_ascii=False, allow_nan=False
    ).replace("<", "\\u003c")
    first = routes[0]
    page = template.replace("__ROUTE_DATA__", payload).replace(
        "__STATIC_MAP__",
        static_map(
            first["original"],
            [] if first.get("hybrid") else first["reconstructed"],
            []
            if first.get("hybrid")
            else first.get("candidate", [])
            if not first["reconstructed"]
            else [],
            [] if first.get("hybrid") else first.get("partial_candidates", []),
            display_segments=first.get("hybrid", {}).get("segments", []),
        ),
    )
    for identifier, value in [
        ("original-distance", f"{first['original_m']:.1f} m"),
        (
            "rebuilt-distance",
            f"{first['reconstructed_m']:.1f} m"
            if first["reconstructed_m"] is not None
            else "未通过",
        ),
        ("edge-count", f"{first['edge_count']} 条"),
        ("attempt-count", f"{first['attempt_count']} 次"),
        (
            "match-count",
            f"{first['match_attempt_count']} 次"
            if first.get("match_attempt_count") is not None
            else "未记录",
        ),
    ]:
        page = page.replace(
            f'<strong id="{identifier}"></strong>',
            f'<strong id="{identifier}">{value}</strong>',
        )
    # Script-disabled previews still expose all cases as simple route drawings.
    previews = "".join(
        '<details style="padding:10px"><summary>'
        + html.escape(r["title"])
        + f" · {r['original_m'] / 1000:.2f} km · "
        + (
            "待审核"
            if (
                r.get("hybrid", {}).get("mode") == "road_candidate"
                if r.get("hybrid")
                else bool(r["reconstructed"])
            )
            else "未通过"
        )
        + '</summary><div style="height:420px;position:relative">'
        + static_map(
            r["original"],
            [] if r.get("hybrid") else r["reconstructed"],
            []
            if r.get("hybrid")
            else r.get("candidate", [])
            if not r["reconstructed"]
            else [],
            [] if r.get("hybrid") else r.get("partial_candidates", []),
            display_segments=r.get("hybrid", {}).get("segments", []),
        ).replace(
            'id="fallback-map"',
            'class="fallback-static" style="width:100%;height:100%"',
        )
        + "</div>"
        + (
            "<p>核心指纹："
            + html.escape(r["fingerprint"]["core_fingerprint"])
            + "<br>版本指纹："
            + html.escape(r["fingerprint"]["version_fingerprint"])
            + "<br>POI："
            + html.escape(
                "、".join(
                    p["name"] + " [" + p["id"] + "]"
                    for p in r["fingerprint"]["poi_associations"]
                )
            )
            + "</p>"
            if r.get("fingerprint")
            else ""
        )
        + "</details>"
        for r in routes
    )
    page = page.replace(
        "</main>",
        '</main><noscript><section style="max-width:1100px;margin:auto"><h2>本批全部路线（静态对照）</h2>'
        + previews
        + "</section></noscript>",
    )
    Path(output).write_text(page, encoding="utf-8")
    return len(routes)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("output")
    args = p.parse_args()
    print("Gallery routes:", build(args.source, args.output))


if __name__ == "__main__":
    main()
