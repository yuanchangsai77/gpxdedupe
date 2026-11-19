#!/usr/bin/env python3
"""Loopback-only, standard-library HTTP interface for route_dedupe."""
from __future__ import annotations

import argparse
import hashlib
import io
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
import socket
from pathlib import Path
import tempfile
import threading
from urllib.parse import unquote, urlsplit
import uuid

import route_dedupe
import route_plans
import loop_recognition

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DIR = r"D:\7281\workspace\route\1\komoot\Algeria\Algeria\02_明细\gpx"
MAX_BODY_BYTES = 16 * 1024
MAX_GPX_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000
DOWNLOAD_NAMES = {"gpx": "cleaned.gpx", "geojson": "cleaned.geojson", "report": "report.json", "manifest": "manifest.json"}
MAX_ANALYSES = 8
ANALYSIS_TTL_SECONDS = 30 * 60


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def loopback_host(value):
    # Resolve no user-controlled names: avoid exposing this local file service.
    if value not in ("127.0.0.1", "localhost"):
        raise ValueError("仅允许 127.0.0.1 或 localhost（绑定到 127.0.0.1）")
    return "127.0.0.1"


def has_link(path):
    """Reject symlinks and Windows junctions, including ancestor components."""
    return any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))


def safe_file(path, root):
    if has_link(path) or not path.resolve(strict=True).is_relative_to(root):
        raise APIError(403, "禁止访问符号链接、目录联接或目录外文件")
    if not path.is_file():
        raise APIError(404, "文件不存在")
    return path.stat()


@dataclass(frozen=True)
class FileRecord:
    path: Path
    root: Path
    export: loop_recognition.RouteExportIndex | None = None
    entry: dict | None = None

    @property
    def snapshot_name(self):
        return self.path.name if self.export is None else f"route-{self.entry['source_index']}.gpx"


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=8765, *, web_dir=None, output_dir=None):
        self.web_dir = Path(web_dir if web_dir is not None else PROJECT_DIR / "web").absolute()
        self.output_dir = Path(output_dir if output_dir is not None else PROJECT_DIR / "outputs").absolute()
        self.files: dict[str, FileRecord] = {}
        self.downloads: dict[str, FileRecord] = {}
        self.state_lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.analyses: dict[str, dict] = {}
        super().__init__((loopback_host(host), port), Handler)

    def list_files(self, body):
        legacy = "paths" not in body
        paths = [body.get("folder")] if legacy else body["paths"]
        recursive = body.get("recursive", False)
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) or not path.strip() or "\x00" in path for path in paths)):
            raise APIError(400, "请输入有效的文件夹或路线文件路径")
        if not isinstance(recursive, bool):
            raise APIError(400, "recursive 必须为布尔值")
        files, records, sources, errors = [], {}, [], []
        seen = set()
        truncated = False

        def candidates(source):
            if source.is_file():
                yield source
                return
            for directory, directories, names in os.walk(source, followlinks=False):
                directories[:] = sorted(
                    name for name in directories if not has_link(Path(directory) / name)
                ) if recursive else []
                for name in sorted(names):
                    path = Path(directory) / name
                    if (path.suffix.lower() == ".gpx" or name.lower().endswith(".export.json")) and not has_link(path):
                        yield path

        def add(record, name, relative, size, source):
            nonlocal truncated
            if len(files) >= MAX_FILES:
                truncated = True
                return
            file_id = uuid.uuid4().hex
            records[file_id] = record
            item = {"id": file_id, "name": name, "relative_path": relative, "size_bytes": size}
            if not legacy:
                item.update(source=str(source), format="JSON" if record.export else "GPX")
            files.append(item)

        for raw in paths:
            try:
                source = Path(raw).expanduser().absolute()
                if has_link(source):
                    raise APIError(403, "禁止选择符号链接或目录联接")
                source = source.resolve(strict=True)
                if legacy and not source.is_dir():
                    raise APIError(400, "folder 必须指向目录")
                if source in sources:
                    continue
                sources.append(source)
                root = source if source.is_dir() else source.parent
                for path in candidates(source):
                    if path in seen:
                        continue
                    try:
                        stat = safe_file(path, root)
                        relative = path.relative_to(root).as_posix()
                        if path.suffix.lower() == ".gpx":
                            add(FileRecord(path, root), path.name, relative, stat.st_size, source)
                        elif path.suffix.lower() == ".json":
                            index = loop_recognition.RouteExportIndex(path)
                            supported = [entry for entry in index.entries if entry["schema_supported"]]
                            if not supported:
                                raise APIError(400, "没有可读取的 TopRoute 路线")
                            for entry in supported:
                                number = entry["source_index"]
                                identifier = entry["topRouteId"]
                                name = f"路线 {number}" + (f" · {identifier}" if identifier else "")
                                record = FileRecord(path, root, index, entry)
                                export_path = relative if source.is_dir() else ""
                                add(record, name, "/".join(filter(None, (export_path, f"路线 {number}"))),
                                    entry.get("byte_length"), source)
                                if truncated:
                                    break
                        else:
                            raise APIError(400, "支持 GPX 和 TopRoute JSON 文件")
                        seen.add(path)
                    except (APIError, loop_recognition.ExportError, OSError) as exc:
                        errors.append(f"{path}：{exc}")
                    if truncated:
                        break
            except (APIError, OSError) as exc:
                if legacy:
                    raise
                errors.append(f"{raw}：{exc if isinstance(exc, APIError) else exc.strerror}")
            if truncated:
                break
        with self.state_lock:
            self.files.update(records)
        return {"folder": str(sources[0]) if len(sources) == 1 else "\n".join(map(str, sources)),
                "files": files, "truncated": truncated, "errors": errors}

    def source_record(self, file_id):
        if not isinstance(file_id, str):
            raise APIError(400, "file_id 必须是列表返回的文件标识")
        with self.state_lock:
            record = self.files.get(file_id)
        if record is None:
            raise APIError(404, "文件标识不存在，请重新列出目录")
        return record

    def read_source(self, record, max_bytes=MAX_GPX_BYTES):
        stat = safe_file(record.path, record.root)
        if record.export is not None:
            return self.export_gpx(record, max_bytes)
        limit_message = f"GPX 文件不能超过 {max_bytes // (1024 * 1024)} MiB"
        if stat.st_size > max_bytes:
            raise APIError(413, limit_message)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(record.path, flags), "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (stat.st_dev, stat.st_ino):
                raise APIError(403, "文件已变化，请重新列出目录")
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise APIError(413, limit_message)
        xml_text = data.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in xml_text or b"<!ENTITY" in xml_text:
            raise APIError(400, "GPX 不允许包含 DTD 或实体声明")
        return data

    def export_gpx(self, record, max_bytes):
        try:
            route = record.export.load(record.entry)
        except loop_recognition.ExportError as exc:
            raise APIError(409, str(exc)) from exc
        segments = route.get("segments") if isinstance(route, dict) else None
        if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
            raise APIError(400, "TopRoute 路线必须包含一个连续轨迹段")
        points = segments[0].get("routePoints")
        if not isinstance(points, list) or not 3 <= len(points) <= loop_recognition.LoopRecognitionConfig().max_points:
            raise APIError(400, "TopRoute 轨迹点数超出支持范围")
        coords, elevations = [], []
        for point in points:
            if not isinstance(point, dict):
                raise APIError(400, "TopRoute 轨迹点格式无效")
            lon, lat, altitude = (point.get(key) for key in ("longitude", "latitude", "altitude"))
            if not loop_recognition._valid_export_numbers(lon, lat, altitude) or abs(lon) > 180 or abs(lat) > 90:
                raise APIError(400, "TopRoute 轨迹点坐标或海拔无效")
            coords.append((lon, lat))
            elevations.append(altitude if altitude is not None else math.nan)
        stream = io.BytesIO()
        route_dedupe.write_gpx(stream, coords, elevations, record.snapshot_name, full_precision=True)
        data = stream.getvalue()
        if len(data) > max_bytes:
            raise APIError(413, "路线超过输入字节限制")
        return data

    def preview_file(self, body):
        record = self.source_record(body.get("file_id"))
        coords, _, _, _ = route_dedupe.load_gpx(io.BytesIO(self.read_source(record)))
        if (len(coords) > loop_recognition.LoopRecognitionConfig().max_points
                or not all(math.isfinite(lon) and math.isfinite(lat) and abs(lon) <= 180 and abs(lat) <= 90
                           for lon, lat in coords)):
            raise APIError(400, "路线坐标或点数无效")
        return {"original": coords.tolist()}

    def prepare_output(self):
        if has_link(self.output_dir):
            raise APIError(403, "输出目录不允许使用符号链接或目录联接")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def register_downloads(self, run_id, out, names):
        urls = {kind: f"/api/download/{run_id}/{kind}" for kind in names}
        with self.state_lock:
            self.downloads.update({url: FileRecord(out / names[kind], out.resolve())
                                   for kind, url in urls.items()})
        return urls

    def analyze_file(self, body):
        radius = body.get("radius", 18)
        if isinstance(radius, bool) or not isinstance(radius, (int, float)):
            raise APIError(400, "radius 必须是数值")
        if not math.isfinite(radius) or not 0 < radius <= 1000:
            raise APIError(400, "radius 必须大于 0 且不超过 1000 米")
        record = self.source_record(body.get("file_id"))
        # Admission happens before reading any large snapshot, not after allocation.
        if not self.run_lock.acquire(blocking=False):
            raise APIError(429, "正在分析或导出另一条路线，请完成后重试")
        try:
            data = self.read_source(record)
            self.prepare_output()
            analysis_id = uuid.uuid4().hex
            out = self.output_dir / analysis_id
            with tempfile.TemporaryDirectory(prefix="analysis-", dir=self.output_dir) as tmp:
                snapshot = Path(tmp) / record.snapshot_name
                snapshot.write_bytes(data)
                analysis = route_plans.analyze(snapshot, route_dedupe.Config(match_radius_m=float(radius)))
                report = {**analysis["report"], "schema_version": analysis["schema_version"],
                          "input_sha256": analysis["input_sha256"], "plan_digest": analysis["plan_digest"],
                          "phase": "analysis", "confirmed": False, "road_identity_verified": False,
                          "candidate": analysis["candidate"]}
                if analysis["candidate"] and analysis["candidate"]["blocked_reasons"]:
                    # The unsafe legacy proposal is evidence, not the displayed preview.
                    report["proposed_output"] = {key: report[key] for key in
                                                 ("method", "output_points", "output_length_m", "removed_length_m", "output_closed")}
                    report.update(method="unchanged", output_points=report["input_points"],
                                  output_length_m=report["input_length_m"], removed_length_m=0.,
                                  output_closed=report["start_end_distance_m"] < .1)
                response = {"analysis_id": analysis_id, "plan_digest": analysis["plan_digest"],
                            "report": report, "candidate": analysis["candidate"],
                            "original": analysis["original"], "cleaned": analysis["preview"],
                            "downloads": {"report": f"/api/download/{analysis_id}/report"}}
                json.dumps(response, allow_nan=False)
                published = Path(tmp) / "published"
                published.mkdir()
                (published / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
                published.rename(out)
            now = time.monotonic()
            with self.state_lock:
                expired = [key for key, value in self.analyses.items()
                           if now - value["created"] >= ANALYSIS_TTL_SECONDS]
                for key in expired:
                    del self.analyses[key]
                while len(self.analyses) >= MAX_ANALYSES:
                    del self.analyses[next(iter(self.analyses))]
                self.analyses[analysis_id] = {"analysis": analysis, "record": record, "created": now}
            self.register_downloads(analysis_id, out, {"report": "report.json"})
            return response
        finally:
            self.run_lock.release()

    def classify_file(self, body):
        record = self.source_record(body.get("file_id"))
        if not self.run_lock.acquire(blocking=False):
            raise APIError(429, "正在分析或导出另一条路线，请完成后重试")
        try:
            config = loop_recognition.RouteShapeConfig()
            if record.export is None:
                result = loop_recognition.classify_route_bytes(
                    self.read_source(record, max_bytes=config.max_input_bytes), config)
            else:
                safe_file(record.path, record.root)
                try:
                    route = record.export.load(record.entry)
                except loop_recognition.ExportError as exc:
                    raise APIError(409, str(exc)) from exc
                result = loop_recognition.classify_export_record(route, config)
            return self.publish_classification_report(result)
        finally:
            self.run_lock.release()

    def publish_classification_report(self, result):
        report_id = uuid.uuid4().hex
        report = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        self.prepare_output()
        out = self.output_dir / report_id
        with tempfile.TemporaryDirectory(prefix="report-", dir=self.output_dir) as tmp:
            published = Path(tmp) / "published"
            published.mkdir()
            (published / "report.json").write_text(report, encoding="utf-8")
            published.rename(out)
        self.register_downloads(report_id, out, {"report": "report.json"})
        return {**result, "classification_id": report_id,
                "downloads": {"report": f"/api/download/{report_id}/report"}}

    def run_file(self, body):
        # Old clients cannot bypass P0: /api/run is now an analyze-only alias.
        return self.analyze_file(body)

    def apply_file(self, body):
        analysis_id = body.get("analysis_id")
        if not isinstance(analysis_id, str):
            raise APIError(400, "必须提供分析标识，请先分析路线")
        if not self.run_lock.acquire(blocking=False):
            raise APIError(429, "正在分析或导出另一条路线，请完成后重试")
        try:
            with self.state_lock:
                entry = self.analyses.get(analysis_id)
            if entry is None or time.monotonic() - entry["created"] >= ANALYSIS_TTL_SECONDS:
                raise APIError(409, "分析已过期或不存在，请重新分析")
            analysis = entry["analysis"]
            candidate = analysis["candidate"]
            if candidate is None or candidate.get("blocked_reasons"):
                raise APIError(409, "没有可执行候选或候选已被安全检查阻止，请保留原轨迹")
            if body.get("plan_digest") != analysis["plan_digest"]:
                raise APIError(409, "计划版本不匹配，请重新分析")
            if body.get("goal") != candidate["goal"]:
                raise APIError(400, "处理目标必须与当前候选一致")
            if body.get("confirm") is not True or body.get("acknowledge_geometry_only") is not True:
                raise APIError(400, "必须明确确认候选及仅几何证据的限制")
            if candidate["goal"] == "main_loop" and body.get("acknowledge_access_loss") is not True:
                raise APIError(400, "主体环裁切必须明确同意删除两侧接入行程")
            data = self.read_source(entry["record"])
            if hashlib.sha256(data).hexdigest() != analysis["input_sha256"]:
                raise APIError(409, "源文件已变化，请重新分析并确认")
            self.prepare_output()
            run_id = uuid.uuid4().hex
            out = self.output_dir / run_id
            with tempfile.TemporaryDirectory(prefix="apply-", dir=self.output_dir) as tmp:
                snapshot = Path(tmp) / entry["record"].snapshot_name
                snapshot.write_bytes(data)
                published = Path(tmp) / "published"
                report, original, cleaned = route_plans.apply(
                    snapshot, published, analysis, goal=body.get("goal"),
                    plan_digest=body.get("plan_digest"), confirm=body.get("confirm"),
                    acknowledge_access_loss=body.get("acknowledge_access_loss"),
                    acknowledge_geometry_only=body.get("acknowledge_geometry_only"))
                response = {"run_id": run_id, "report": report,
                            "original": original.tolist() if hasattr(original, "tolist") else original,
                            "cleaned": cleaned.tolist() if hasattr(cleaned, "tolist") else cleaned,
                            "downloads": {kind: f"/api/download/{run_id}/{kind}" for kind in DOWNLOAD_NAMES}}
                json.dumps(response, allow_nan=False)
                for name in DOWNLOAD_NAMES.values():
                    if not (published / name).is_file():
                        raise APIError(500, "导出制品不完整，未发布结果")
                published.rename(out)
            self.register_downloads(run_id, out, DOWNLOAD_NAMES)
            return response
        finally:
            self.run_lock.release()


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "GPXLocal/0.1"

    def finish(self):
        try:
            super().finish()
        finally:
            # HTTP/1.0 responses close the connection. An early validation error
            # may leave request bytes in flight; closing with unread data can
            # reset TCP on Windows before the client receives the JSON error.
            # Flush/FIN the response first, then discard only a bounded amount
            # of input. Never trust invalid framing or wait for an entire body.
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(.25)
                remaining = 64 * 1024
                deadline = time.monotonic() + .25
                while remaining > 0 and time.monotonic() < deadline:
                    self.connection.settimeout(max(.001, deadline - time.monotonic()))
                    data = self.connection.recv(min(8192, remaining))
                    if not data:
                        break
                    remaining -= len(data)
            except OSError:
                pass

    def log_message(self, format, *args):
        # Do not print untrusted filesystem names or HTTP request text to the terminal.
        pass

    def send_error(self, code, message=None, explain=None):
        self.send_json(code, {"error": "请求方法不支持" if code == 501 else "请求格式错误"})

    def send_bytes(self, status, data, content_type, *, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status, data):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def validate_origin(self):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update(("127.0.0.1", "localhost"))
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            raise APIError(403, "Host 必须为当前本机服务地址")
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0].lower() != "http://" + hosts[0].lower()):
            raise APIError(403, "禁止跨域请求，请从当前本机网页操作")
        # Sec-Fetch-Site covers browser requests with Origin omitted.
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise APIError(403, "禁止跨域请求")

    def read_json(self):
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise APIError(415, "请求 Content-Type 必须为 application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            raise APIError(400, "必须提供唯一的 Content-Length，不支持分块请求")
        if not lengths[0].isascii() or not lengths[0].isdigit():
            raise APIError(400, "Content-Length 无效")
        size = int(lengths[0])
        if size > MAX_BODY_BYTES:
            raise APIError(413, "JSON 请求不能超过 16KB")
        data = self.rfile.read(size)
        if len(data) != size:
            raise APIError(400, "JSON 请求内容不完整")
        try:
            def reject_constant(value):
                raise ValueError(value)
            body = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, "JSON 格式无效，请使用 UTF-8 编码") from None
        if not isinstance(body, dict):
            raise APIError(400, "JSON 请求必须是对象")
        return body

    def request_path(self):
        raw = urlsplit(self.path)
        if raw.scheme or raw.netloc:
            raise APIError(400, "不支持绝对请求地址")
        try:
            path = unquote(raw.path, errors="strict")
        except UnicodeError:
            raise APIError(400, "请求路径编码无效") from None
        if any(c in path for c in ("\\", "\x00", ":")) or any(p in (".", "..") for p in path.split("/")):
            raise APIError(403, "禁止目录遍历")
        return path

    def get(self, path):
        if path == "/api/config":
            return self.send_json(200, {"sample_dir": str((PROJECT_DIR / "examples").resolve()),
                                        "default_dir": DEFAULT_DIR, "version": "0.1"})
        if path.startswith("/api/download/"):
            with self.server.state_lock:
                record = self.server.downloads.get(path)
            if record is None:
                raise APIError(404, "下载不存在或服务已重启")
            safe_file(record.path, record.root)
            types = {".gpx": "application/gpx+xml", ".geojson": "application/geo+json",
                     ".json": "application/json"}
            return self.send_bytes(200, record.path.read_bytes(), types[record.path.suffix],
                                   filename=record.path.name)
        if path.startswith("/api/"):
            raise APIError(404, "API 不存在")
        relative = {"/": "index.html", "/about": "about.html"}.get(path, path.lstrip("/"))
        root = self.server.web_dir.resolve()
        target = self.server.web_dir / relative
        safe_file(target, root)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), content_type)

    def dispatch(self):
        self.connection.settimeout(15)
        try:
            self.validate_origin()
            path = self.request_path()
            if self.command in ("GET", "HEAD"):
                self.get(path)
            elif self.command == "POST":
                body = self.read_json()
                if path == "/api/files":
                    result = self.server.list_files(body)
                elif path == "/api/preview":
                    result = self.server.preview_file(body)
                elif path in ("/api/run", "/api/analyze"):
                    result = self.server.analyze_file(body)
                elif path == "/api/classify":
                    result = self.server.classify_file(body)
                elif path == "/api/apply":
                    result = self.server.apply_file(body)
                else:
                    raise APIError(404, "API 不存在")
                self.send_json(200, result)
            else:
                raise APIError(405, "请求方法不支持，禁止跨域预检")
        except APIError as error:
            self.send_json(error.status, {"error": str(error)})
        except loop_recognition.RecognitionError as error:
            self.send_json(400, {"error": f"环识别失败：{error}"})
        except route_plans.PlanError as error:
            self.send_json(400, {"error": str(error)})
        except FileNotFoundError:
            self.send_json(404, {"error": "文件或目录不存在"})
        except PermissionError:
            self.send_json(403, {"error": "没有权限访问文件或目录"})
        except (ValueError, KeyError, OverflowError, route_dedupe.ET.ParseError):
            self.send_json(400, {"error": "参数或 GPX 内容无效；仅支持至少三个点的单轨迹段"})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except OSError:
            self.send_json(400, {"error": "文件访问失败，请重新列出目录后重试"})
        except Exception:
            self.send_json(500, {"error": "处理失败，请检查 GPX 数据后重试"})

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_OPTIONS = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch
    do_PATCH = dispatch


def create_server(host="127.0.0.1", port=8765, **kwargs):
    return LocalServer(host, port, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地 GPX 网页（仅绑定 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    try:
        host = loopback_host(args.host)
    except ValueError as error:
        parser.error(str(error))
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    with create_server(host, args.port) as server:
        print(f"GPX 本地网页：http://127.0.0.1:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
