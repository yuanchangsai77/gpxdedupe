#!/usr/bin/env python3
"""Loopback-only, standard-library HTTP interface for route_dedupe."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import unquote, urlsplit
import uuid

import route_dedupe

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DIR = r"D:\7281\workspace\route\1\komoot\Algeria\Algeria\02_明细\gpx"
MAX_BODY_BYTES = 16 * 1024
MAX_GPX_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000
DOWNLOAD_NAMES = {"gpx": "cleaned.gpx", "geojson": "cleaned.geojson", "report": "report.json"}


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


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=8765, *, web_dir=None, output_dir=None):
        self.web_dir = Path(web_dir if web_dir is not None else PROJECT_DIR / "web").absolute()
        self.output_dir = Path(output_dir if output_dir is not None else PROJECT_DIR / "outputs").absolute()
        self.files: dict[str, FileRecord] = {}
        self.downloads: dict[str, FileRecord] = {}
        self.state_lock = threading.Lock()
        self.run_lock = threading.Lock()
        super().__init__((loopback_host(host), port), Handler)

    def list_files(self, body):
        folder = body.get("folder")
        recursive = body.get("recursive", False)
        if not isinstance(folder, str) or not folder.strip() or "\x00" in folder:
            raise APIError(400, "folder 必须是非空目录路径")
        if not isinstance(recursive, bool):
            raise APIError(400, "recursive 必须为布尔值")
        root = Path(folder).expanduser().absolute()
        if has_link(root):
            raise APIError(403, "禁止选择符号链接或目录联接")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise APIError(400, "folder 必须指向目录")
        files = []
        records = {}
        truncated = False

        def walk_error(error):
            raise error

        for directory, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
            directories[:] = sorted(
                name for name in directories
                if not has_link(Path(directory) / name)
            ) if recursive else []
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix.lower() != ".gpx" or has_link(path):
                    continue
                stat = safe_file(path, root)
                if len(files) == MAX_FILES:
                    truncated = True
                    break
                file_id = uuid.uuid4().hex
                records[file_id] = FileRecord(path.resolve(strict=True), root)
                files.append({"id": file_id, "name": path.name,
                              "relative_path": path.relative_to(root).as_posix(),
                              "size_bytes": stat.st_size})
            if truncated:
                break
        with self.state_lock:
            self.files.update(records)
        return {"folder": str(root), "files": files, "truncated": truncated}

    def run_file(self, body):
        file_id = body.get("file_id")
        radius = body.get("radius", 18)
        if not isinstance(file_id, str):
            raise APIError(400, "file_id 必须是列表返回的文件标识")
        if isinstance(radius, bool) or not isinstance(radius, (int, float)):
            raise APIError(400, "radius 必须是数值")
        if not math.isfinite(radius) or not 0 < radius <= 1000:
            raise APIError(400, "radius 必须大于 0 且不超过 1000 米")
        with self.state_lock:
            record = self.files.get(file_id)
        if record is None:
            raise APIError(404, "文件标识不存在，请重新列出目录")
        stat = safe_file(record.path, record.root)
        if stat.st_size > MAX_GPX_BYTES:
            raise APIError(413, "GPX 文件不能超过 50MB")
        # Work on a bounded snapshot, not a path the caller can change while run parses it.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(record.path, flags), "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (stat.st_dev, stat.st_ino):
                raise APIError(403, "文件已变化，请重新列出目录")
            data = source.read(MAX_GPX_BYTES + 1)
        if len(data) > MAX_GPX_BYTES:
            raise APIError(413, "GPX 文件不能超过 50MB")
        # Expat accepts UTF-16/32 as well; strip NULs for the declaration check.
        xml_text = data.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in xml_text or b"<!ENTITY" in xml_text:
            raise APIError(400, "GPX 不允许包含 DTD 或实体声明")
        if has_link(self.output_dir):
            raise APIError(403, "输出目录不允许使用符号链接或目录联接")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        out = self.output_dir / run_id
        out.mkdir()
        try:
            with self.run_lock, tempfile.TemporaryDirectory(prefix="input-", dir=out) as tmp:
                snapshot = Path(tmp) / record.path.name
                snapshot.write_bytes(data)
                original, _, _, _ = route_dedupe.load_gpx(snapshot)
                if any(not math.isfinite(float(lon)) or not math.isfinite(float(lat))
                       or not -180 <= lon <= 180 or not -90 <= lat <= 90
                       for lon, lat in original):
                    raise APIError(400, "GPX 经纬度必须是有效范围内的有限数值")
                report, _, _ = route_dedupe.run(
                    snapshot, out, route_dedupe.Config(match_radius_m=float(radius)))
                cleaned = json.loads((out / "cleaned.geojson").read_text(encoding="utf-8"))
                response = {"run_id": run_id, "report": report,
                            "original": original.tolist(),
                            "cleaned": cleaned["features"][0]["geometry"]["coordinates"],
                            "downloads": {kind: f"/api/download/{run_id}/{kind}"
                                          for kind in DOWNLOAD_NAMES}}
                # Verify serializability before making any downloads visible.
                json.dumps(response, allow_nan=False)
            with self.state_lock:
                self.downloads.update({url: FileRecord(out / DOWNLOAD_NAMES[kind], out.resolve())
                                       for kind, url in response["downloads"].items()})
            return response
        except Exception:
            shutil.rmtree(out)
            raise


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "GPXLocal/0.1"

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
                elif path == "/api/run":
                    result = self.server.run_file(body)
                else:
                    raise APIError(404, "API 不存在")
                self.send_json(200, result)
            else:
                raise APIError(405, "请求方法不支持，禁止跨域预检")
        except APIError as error:
            self.send_json(error.status, {"error": str(error)})
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
