"""Opt-in safety plans around route_dedupe.run (the unchanged legacy engine).

Only geometry is compared. These conservative gates reject known ambiguous
patterns, not all possible out-and-backs or distinct parallel roads. Point,
resample and spatial-pair limits are admission checks, NOT a hard process budget.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import hashlib
import json
import math
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

try:
    from . import route_dedupe as rd
except ImportError:  # Also support the legacy project's direct-module imports.
    import route_dedupe as rd

SCHEMA_VERSION = "1.0"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_POINTS = 50_000
MAX_SAMPLES = 12_000
MAX_SPATIAL_PAIRS = 250_000
CAUTIONS = [
    "GPX geometry matching does not establish that observations use the same road; road identity is not verified.",
    "Known-pattern safety gates are conservative heuristics, not general reliable route or road identification.",
    "The preview may snap endpoints; inspect against roads before navigation. Original timestamps are omitted.",
]
OUTPUT_NAMES = ("cleaned.gpx", "cleaned.geojson", "report.json", "manifest.json")


class PlanError(ValueError):
    """Invalid plan; messages are safe for a Chinese HTTP client."""

    _messages = {
        "Plan must contain finite JSON-compatible values": "计划必须是有限数值的有效 JSON 数据。",
        "cfg must be Config or a complete config dict": "配置必须是 Config 或完整配置对象。",
        "Config must contain exactly the supported fields": "配置字段不完整或包含不支持的字段。",
        "allow_bridge_m must be in [0, 100000]": "端点调整上限必须介于 0 和 100000 米之间。",
        "max_tail_m must be in (0, 1000000] or None": "尾段上限必须介于 0（不含）和 1000000 米之间，或为空。",
        "Cannot read input GPX": "无法读取输入 GPX 文件。",
        "Input byte limit exceeded": "输入文件超过大小限制。",
        "DTD/entities and NUL-encoded XML are not supported": "不支持 DTD、实体声明或含 NUL 的 XML 编码。",
        "Exactly one track segment is required": "仅支持恰好一个轨迹段。",
        "Coordinates must be finite valid longitude/latitude": "经纬度必须是有限数值且在有效范围内。",
        "Elevation must be finite when supplied": "提供的海拔必须是有限数值。",
        "Projection produced nonfinite coordinates": "坐标投影无效，无法安全分析。",
        "Track must have positive finite length": "轨迹长度必须是正的有限数值。",
        "Resampled point limit exceeded; increase spacing or shorten input": "重采样点数超过限制，请增大采样间距或缩短轨迹。",
        "Spatial pair limit exceeded": "空间匹配对数超过限制，轨迹过于密集。",
        "Malformed or invalid GPX": "GPX 格式或数据无效。",
        "Legacy geometry analysis failed": "几何分析失败，无法生成安全计划。",
        "Explicit confirmation and geometry-only acknowledgment required": "必须明确确认计划，并确认仅进行了几何匹配、未验证道路身份。",
        "Explicit supported goal required": "必须明确选择受支持的转换目标。",
        "Unsupported analysis schema": "分析结果的版本无效或不受支持。",
        "Plan digest mismatch": "计划摘要不匹配，请重新分析并确认。",
        "Missing analysis config": "分析结果缺少完整配置。",
        "Input SHA-256 changed": "输入文件已变化，请重新分析并确认。",
        "Analysis was changed or is stale; analyze again": "分析结果被修改或已过期，请重新分析。",
        "No applicable unblocked candidate": "没有可应用的候选计划，或计划已被安全检查阻止。",
        "Goal does not match the candidate": "所选目标与候选计划不一致。",
        "Removal of both access legs requires acknowledgment": "必须明确确认删除进出接入段，包括没有反向匹配证据的部分。",
        "Output files already exist; use a new output directory": "输出文件已存在，请使用新的输出目录。",
        "Output must not overwrite the input": "输出不得覆盖输入文件。",
        "Input changed during verification": "验证期间输入文件发生变化，请重新分析。",
        "Cannot publish output files": "无法生成输出文件。",
    }

    def __init__(self, message):
        translated = self._messages.get(message)
        if translated is None:
            # Variable-field errors contain only our fixed field names/numbers,
            # never a file path or a lower-level exception message.
            if message.startswith("Config "):
                translated = f"配置参数 {message.split()[1]} 无效；请检查有限数值和允许范围。"
            elif message.endswith(" must be an exact bool"):
                translated = f"确认参数 {message.split()[0]} 必须是明确的布尔值。"
            elif message.startswith("Input must contain 3.."):
                translated = f"输入必须包含 3 至 {MAX_POINTS} 个轨迹点。"
            else:
                translated = "计划参数无效，无法安全处理。"
        super().__init__(translated)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PlanError("Plan must contain finite JSON-compatible values") from exc


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _config(cfg):
    if cfg is None:
        values = asdict(rd.Config())
    elif isinstance(cfg, rd.Config):
        values = asdict(cfg)
    elif type(cfg) is dict:
        values = cfg.copy()
    else:
        raise PlanError("cfg must be Config or a complete config dict")
    if set(values) != {field.name for field in fields(rd.Config)}:
        raise PlanError("Config must contain exactly the supported fields")
    for key, value in values.items():
        if key == "max_tail_m" and value is None:
            continue
        try:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError
            values[key] = float(value)
        except (ValueError, OverflowError) as exc:
            raise PlanError(f"Config {key} must be a finite number, not bool") from exc
    for key in ("spacing_m", "match_radius_m", "min_overlap_m", "closure_radius_m"):
        if not 0 < values[key] <= 100_000:
            raise PlanError(f"Config {key} must be in (0, 100000]")
    if not 0 <= values["allow_bridge_m"] <= 100_000:
        raise PlanError("allow_bridge_m must be in [0, 100000]")
    for key in ("heading_min_opposite", "min_tail_match_fraction"):
        if not 0 < values[key] <= 1:
            raise PlanError(f"Config {key} must be in (0, 1]")
    if values["max_tail_m"] is not None and not 0 < values["max_tail_m"] <= 1_000_000:
        raise PlanError("max_tail_m must be in (0, 1000000] or None")
    return rd.Config(**values)


def _read(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise PlanError("Cannot read input GPX") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise PlanError("Input byte limit exceeded")
    return raw


def _validate(raw, cfg):
    # Reject declarations before XML parsing; NUL also excludes UTF-16/32
    # encodings that could hide a declaration from this byte-level guard.
    upper = raw.upper()
    if b"\x00" in raw or b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise PlanError("DTD/entities and NUL-encoded XML are not supported")
    try:
        root = ET.fromstring(raw)
        segments = root.findall(".//" + rd.tag("trkseg"))
        if len(segments) != 1:
            raise PlanError("Exactly one track segment is required")
        points = segments[0].findall(rd.tag("trkpt"))
        if not 3 <= len(points) <= MAX_POINTS:
            raise PlanError(f"Input must contain 3..{MAX_POINTS} trackpoints")
        coords = []
        for point in points:
            lon, lat = float(point.attrib["lon"]), float(point.attrib["lat"])
            if not (math.isfinite(lon) and math.isfinite(lat) and
                    -180 <= lon <= 180 and -90 <= lat <= 90):
                raise PlanError("Coordinates must be finite valid longitude/latitude")
            ele = point.findtext(rd.tag("ele"))
            if ele is not None and not math.isfinite(float(ele)):
                raise PlanError("Elevation must be finite when supplied")
            coords.append([lon, lat])
        coords = np.asarray(coords)
        xy, _, _ = rd.projection(coords)
        if not np.isfinite(xy).all():
            raise PlanError("Projection produced nonfinite coordinates")
        length = float(rd.arclength(xy)[-1])
        if not math.isfinite(length) or length <= 0:
            raise PlanError("Track must have positive finite length")
        if math.ceil(length / cfg.spacing_m) + 1 > MAX_SAMPLES:
            raise PlanError("Resampled point limit exceeded; increase spacing or shorten input")
        _, samples = rd.resample(xy, cfg.spacing_m)
        # query_pairs in legacy materializes all pairs. Reject dense inputs first
        # using per-point counts, without allocating the actual pair array.
        counts = rd.cKDTree(samples).query_ball_point(
            samples, cfg.match_radius_m, return_length=True)
        if (int(counts.sum()) - len(samples)) // 2 > MAX_SPATIAL_PAIRS:
            raise PlanError("Spatial pair limit exceeded")
    except PlanError:
        raise
    except (ET.ParseError, KeyError, TypeError, ValueError, OverflowError, LookupError) as exc:
        raise PlanError("Malformed or invalid GPX") from exc
    return coords


def _middle_blocks(s, q, a, b, cfg):
    probes = np.linspace(a, b, 301)
    middle = np.column_stack([np.interp(probes, s, q[:,axis]) for axis in range(2)])
    centered = middle - middle.mean(axis=0)
    _, singular, axes = np.linalg.svd(centered, full_matrices=False)
    lateral = centered @ axes[-1]
    width = float(np.quantile(lateral, .95) - np.quantile(lateral, .05))
    reasons = []
    if singular[0] < 1 or width <= max(30., 2 * cfg.match_radius_m) or singular[-1] / max(singular[0], 1.) < .025:
        reasons.append("retained_middle_near_linear_or_degenerate")
    # Reverse correspondence catches curved retraces as well, with more slack
    # than legacy's known exact-out-and-back check.
    reverse = middle[::-1]
    if float(np.mean(np.linalg.norm(middle - reverse, axis=1) <= 1.5 * cfg.match_radius_m)) >= .75:
        reasons.append("retained_middle_retrace_evidence")
    return reasons


def _parallel_block(s, q, a, b, cfg):
    first = np.flatnonzero((s >= cfg.spacing_m * 3) & (s <= a - cfg.spacing_m * 3))
    last = np.flatnonzero(s >= b)
    if len(first) < 10 or len(last) < 10:
        return False
    distance, nearest = rd.cKDTree(q[last]).query(q[first])
    targets = last[nearest]
    directions = rd.tangent(q)
    good = (distance <= cfg.match_radius_m) & (np.sum(directions[first] * directions[targets], axis=1) <= -cfg.heading_min_opposite)
    if good.sum() < 10 or good.mean() < .7:
        return False
    source = first[good]
    delta = q[targets[good]] - q[source]
    lateral = directions[source, 0] * delta[:,1] - directions[source, 1] * delta[:,0]
    median = float(np.median(lateral))
    iqr = float(np.quantile(lateral, .75) - np.quantile(lateral, .25))
    stable_sign = float(np.mean(lateral * median > 0))
    span = float(s[source[-1]] - s[source[0]])
    if span < cfg.min_overlap_m or abs(median) < 3.:
        return False
    if stable_sign >= .85 and iqr <= max(2., abs(median) * .35):
        return True
    # Individual nearest-neighbour residuals become wide under independent
    # jitter: an IQR gate alone incorrectly accepts displaced parallel access.
    # Check sustained signed displacement in separated arc windows instead.
    # This is ambiguity evidence only, not inference of physical road identity.
    window_m = max(150., cfg.min_overlap_m, 10 * cfg.spacing_m)
    window_ids = np.floor((s[source] - s[source[0]]) / window_m).astype(int)
    medians = []
    for window in np.unique(window_ids):
        selected = window_ids == window
        positions = s[source[selected]]
        if selected.sum() >= 6 and positions[-1] - positions[0] >= .5 * window_m:
            medians.append(float(np.median(lateral[selected])))
    if len(medians) < 3:
        return False
    sustained = np.asarray(medians) * np.sign(median) >= 2.
    return stable_sign >= .70 and float(np.mean(sustained)) >= .75


def _build(raw, source_name, cfg):
    original = _validate(raw, cfg)
    # Both input snapshot and legacy's publicly named artifacts stay private.
    with tempfile.TemporaryDirectory(prefix="gpx-plan-") as directory:
        private = Path(directory)
        snapshot = private / "input" / source_name
        snapshot.parent.mkdir()
        snapshot.write_bytes(raw)
        try:
            report, xy, result_xy = rd.run(snapshot, private / "legacy", cfg)
        except (ValueError, OverflowError) as exc:
            raise PlanError("Legacy geometry analysis failed") from exc
        artifacts = {name: (private / "legacy" / name).read_bytes()
                     for name in OUTPUT_NAMES[:3]}
        geojson = json.loads(artifacts["cleaned.geojson"])
    candidate = None
    method = report["method"]
    if method in ("reversed_tail_collapse", "repeated_laps_collapse"):
        s, q = rd.resample(xy, cfg.spacing_m)
        tail = report["retraced_tail"]
        if method == "reversed_tail_collapse":
            a, b = tail["cut_start_m"], tail["cut_end_m"]
            goal, unmatched = "main_loop", tail["unmatched_access_m"]
        else:
            a, b = 0., report["identical_laps"]["length_m"]
            goal, unmatched = "one_lap", 0.
        # Use original vertices for exact crop endpoint displacement, not the
        # slightly corner-cutting resampled line used by the safety detectors.
        d = rd.arclength(xy)
        raw_start = np.array([np.interp(a, d, xy[:,axis]) for axis in range(2)])
        raw_end = np.array([np.interp(b, d, xy[:,axis]) for axis in range(2)])
        adjustment = float(np.linalg.norm(result_xy[0] - raw_start) + np.linalg.norm(result_xy[-1] - raw_end))
        blocked = _middle_blocks(s, q, a, b, cfg)
        if tail is not None and _parallel_block(s, q, a, b, cfg):
            blocked.append("stable_lateral_offset_parallel_access_evidence")
        warnings = list(CAUTIONS)
        if goal == "main_loop":
            warnings.append("Both access legs are removed, including unmatched travel; this is not merely deleting a duplicate traversal.")
        candidate = {
            "goal": goal, "method": method, "kept_range_m": [float(a), float(b)],
            "removed_travel_m": max(0., float(d[-1] - (b-a))),
            "unmatched_access_m": float(unmatched),
            "endpoint_adjustment_m": adjustment,
            "warnings": warnings, "blocked_reasons": blocked,
            "requires_confirmation": True,
        }
    report["road_identity_verified"] = False
    report["cautions"] = list(CAUTIONS)
    # A blocked candidate is evidence only: never preview the unsafe transform.
    preview = (original.tolist() if candidate is None or candidate["blocked_reasons"]
               else geojson["features"][0]["geometry"]["coordinates"])
    analysis = {"schema_version": SCHEMA_VERSION,
                "input_sha256": hashlib.sha256(raw).hexdigest(),
                "config": asdict(cfg), "original": original.tolist(),
                "preview": preview, "report": report, "candidate": candidate}
    digest = _digest(analysis)
    analysis.update(analysis_id=digest, plan_digest=digest)
    _json(analysis)
    return analysis, artifacts, geojson


def analyze(path, cfg=None):
    """Return a finite JSON-compatible plan; never publish or modify input.

    cfg is Config, None (defaults), or a dict containing every Config field.
    Coordinates are [longitude, latitude]. Blocked candidates preview the input.
    analysis_id and plan_digest are SHA-256 of the canonical full analysis before
    adding those two identifiers (sorted keys, compact UTF-8, no NaN).
    """
    path = Path(path)
    return _build(_read(path), path.name, _config(cfg))[0]


def apply(path, out_dir, analysis, *, goal, plan_digest, confirm,
          acknowledge_access_loss, acknowledge_geometry_only):
    """Rebuild and verify the entire supplied analysis, then publish four files.

    All three consent arguments must be actual bools. confirm and geometry-only
    acknowledgment must be True; access-loss must be True for main_loop. The
    explicit goal must match the candidate. Existing output files are refused.
    Return (report, original_lonlat, cleaned_lonlat), all JSON-compatible; each
    coordinate is [longitude, latitude]. manifest.json records artifact hashes.
    A digest binds contents, not a user's identity: authentication belongs to the
    caller. Configuration changes require a fresh matching analysis/digest.
    """
    for name, value in (("confirm", confirm), ("acknowledge_access_loss", acknowledge_access_loss),
                        ("acknowledge_geometry_only", acknowledge_geometry_only)):
        if type(value) is not bool:
            raise PlanError(f"{name} must be an exact bool")
    if not confirm or not acknowledge_geometry_only:
        raise PlanError("Explicit confirmation and geometry-only acknowledgment required")
    if goal not in ("main_loop", "one_lap"):
        raise PlanError("Explicit supported goal required")
    if type(analysis) is not dict or analysis.get("schema_version") != SCHEMA_VERSION:
        raise PlanError("Unsupported analysis schema")
    if type(plan_digest) is not str or plan_digest != analysis.get("plan_digest"):
        raise PlanError("Plan digest mismatch")
    _json(analysis)
    if "config" not in analysis:
        raise PlanError("Missing analysis config")
    cfg = _config(analysis["config"])
    path = Path(path)
    raw = _read(path)
    if hashlib.sha256(raw).hexdigest() != analysis.get("input_sha256"):
        raise PlanError("Input SHA-256 changed")
    fresh, artifacts, geojson = _build(raw, path.name, cfg)
    if _json(fresh) != _json(analysis) or plan_digest != fresh["plan_digest"]:
        raise PlanError("Analysis was changed or is stale; analyze again")
    candidate = fresh["candidate"]
    if candidate is None or candidate["blocked_reasons"]:
        raise PlanError("No applicable unblocked candidate")
    if goal != candidate["goal"]:
        raise PlanError("Goal does not match the candidate")
    if goal == "main_loop" and not acknowledge_access_loss:
        raise PlanError("Removal of both access legs requires acknowledgment")
    report = dict(fresh["report"])
    report.update(schema_version=SCHEMA_VERSION, input_sha256=fresh["input_sha256"],
                  goal=goal, plan_digest=plan_digest, confirmed=True,
                  road_identity_verified=False, cautions=candidate["warnings"],
                  kept_range_m=candidate["kept_range_m"],
                  removed_travel_m=candidate["removed_travel_m"],
                  unmatched_access_m=candidate["unmatched_access_m"],
                  endpoint_adjustment_m=candidate["endpoint_adjustment_m"],
                  acknowledge_access_loss=acknowledge_access_loss,
                  acknowledge_geometry_only=acknowledge_geometry_only)
    geojson["features"][0]["properties"].update(
        goal=goal, road_identity_verified=False, plan_digest=plan_digest)
    artifacts["cleaned.geojson"] = _json(geojson).encode("utf-8")
    artifacts["report.json"] = _json(report).encode("utf-8")
    manifest = {"schema_version": SCHEMA_VERSION, "input_sha256": fresh["input_sha256"],
                "analysis_id": fresh["analysis_id"], "plan_digest": plan_digest,
                "config": fresh["config"], "goal": goal, "confirmed": True,
                "road_identity_verified": False, "report": report,
                "artifacts": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                              for name, data in artifacts.items()}}
    artifacts["manifest.json"] = _json(manifest).encode("utf-8")
    output = Path(out_dir)
    if any((output / name).exists() or (output / name).is_symlink() for name in OUTPUT_NAMES):
        raise PlanError("Output files already exist; use a new output directory")
    if any((output / name).resolve() == path.resolve() for name in OUTPUT_NAMES):
        raise PlanError("Output must not overwrite the input")
    if _read(path) != raw:
        raise PlanError("Input changed during verification")
    written = []
    try:
        output.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            target = output / name
            with target.open("xb") as stream:
                written.append(target)
                stream.write(data)
    except OSError as exc:
        for target in written:
            target.unlink(missing_ok=True)
        raise PlanError("Cannot publish output files") from exc
    return report, fresh["original"], fresh["preview"]
