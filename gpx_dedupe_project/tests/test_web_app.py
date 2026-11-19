import http.client
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_app

MINIMAL_GPX = '''<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
<metadata><name>本地测试</name></metadata><trk><trkseg>
<trkpt lat="36.75" lon="3.05"><ele>10</ele></trkpt>
<trkpt lat="36.751" lon="3.051"><ele>20</ele></trkpt>
<trkpt lat="36.752" lon="3.052"><ele>30</ele></trkpt>
</trkseg></trk></gpx>'''


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.web = cls.root / "web"
        cls.web.mkdir()
        (cls.web / "index.html").write_text("<h1>首页</h1>", encoding="utf-8")
        (cls.web / "about.html").write_text("<h1>说明</h1>", encoding="utf-8")
        (cls.web / "app.js").write_text("'use strict';", encoding="utf-8")
        cls.output = cls.root / "outputs"
        cls.server = web_app.create_server(port=0, web_dir=cls.web, output_dir=cls.output)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def setUp(self):
        self.data_temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.data_temp.cleanup)
        self.folder = Path(self.data_temp.name)
        (self.folder / "路线.GPX").write_text(MINIMAL_GPX, encoding="utf-8")
        (self.folder / "ignored.txt").write_text("not GPX", encoding="utf-8")
        sub = self.folder / "sub"
        sub.mkdir()
        (sub / "nested.gpx").write_text(MINIMAL_GPX, encoding="utf-8")

    def request(self, method="GET", path="/api/config", body=None, headers=None, raw=None):
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json"} if raw is not None else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            parsed = json.loads(data) if response_headers.get("Content-Type", "").startswith("application/json") else data
            return response.status, response_headers, parsed
        finally:
            connection.close()

    def files(self, **kwargs):
        return self.request("POST", "/api/files", {"folder": str(self.folder), **kwargs})

    def run_route(self):
        status, _, listing = self.files()
        self.assertEqual(status, 200)
        return self.request("POST", "/api/run", {"file_id": listing["files"][0]["id"]})

    def assert_error(self, result, status):
        actual, headers, body = result
        self.assertEqual(actual, status, body)
        self.assertEqual(set(body), {"error"})
        self.assertTrue(any("\u4e00" <= c <= "\u9fff" for c in body["error"]))
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_config(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"sample_dir": str((web_app.PROJECT_DIR / "examples").resolve()),
                                "default_dir": web_app.DEFAULT_DIR, "version": "0.1"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(Path(body["sample_dir"]).is_absolute())

    def test_static_routes_and_no_cache(self):
        for path, expected in (("/", "首页"), ("/about", "说明"), ("/app.js", "use strict")):
            with self.subTest(path=path):
                status, headers, body = self.request(path=path)
                self.assertEqual(status, 200)
                self.assertIn(expected.encode(), body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assert_error(self.request(path="/missing.html"), 404)
        status, _, body = self.request("HEAD", "/")
        self.assertEqual((status, body), (200, b""))

    def test_list_default_nonrecursive_and_metadata(self):
        status, _, body = self.files()
        self.assertEqual(status, 200)
        self.assertEqual(body["folder"], str(self.folder.resolve()))
        self.assertFalse(body["truncated"])
        self.assertEqual(len(body["files"]), 1)
        entry = body["files"][0]
        self.assertEqual(set(entry), {"id", "name", "relative_path", "size_bytes"})
        self.assertEqual(entry["name"], "路线.GPX")
        self.assertEqual(entry["relative_path"], "路线.GPX")
        self.assertEqual(entry["size_bytes"], (self.folder / "路线.GPX").stat().st_size)
        self.assertNotIn(str(self.folder), entry["id"])
        self.assertEqual(len(entry["id"]), 32)

    def test_recursive(self):
        status, _, body = self.files(recursive=True)
        self.assertEqual(status, 200)
        self.assertEqual({x["relative_path"] for x in body["files"]}, {"路线.GPX", "sub/nested.gpx"})

    def test_cap_at_2000(self):
        for number in range(2000):
            (self.folder / f"{number:04}.gpx").touch()
        status, _, body = self.files()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["files"]), 2000)
        self.assertTrue(body["truncated"])
        (self.folder / "路线.GPX").unlink()
        self.assertFalse(self.files()[2]["truncated"])

    def test_empty_directory(self):
        empty = self.folder / "empty"
        empty.mkdir()
        status, _, body = self.request("POST", "/api/files", {"folder": str(empty)})
        self.assertEqual((status, body["files"], body["truncated"]), (200, [], False))

    def test_invalid_listing(self):
        for folder in (None, 2, "", "\x00"):
            self.assert_error(self.request("POST", "/api/files", {"folder": folder}), 400)
        self.assert_error(self.files(recursive="true"), 400)
        self.assert_error(self.request("POST", "/api/files", {"folder": str(self.folder / "absent")}), 404)
        self.assert_error(self.request("POST", "/api/files", {"folder": str(self.folder / "路线.GPX")}), 400)

    def test_run_alias_is_analysis_only(self):
        status, _, result = self.run_route()
        self.assertEqual(status, 200, result)
        self.assertEqual(set(result), {"analysis_id", "plan_digest", "candidate", "report", "original", "cleaned", "downloads"})
        self.assertEqual(result["original"], [[3.05, 36.75], [3.051, 36.751], [3.052, 36.752]])
        self.assertEqual(result["report"]["input_points"], 3)
        self.assertEqual(result["report"]["source"], "路线.GPX")
        self.assertEqual(result["report"]["config"]["match_radius_m"], 18)
        self.assertEqual(result["report"]["phase"], "analysis")
        self.assertIs(result["report"]["confirmed"], False)
        self.assertEqual(set(result["downloads"]), {"report"})
        out = self.output / result["analysis_id"]
        self.assertEqual({p.name for p in out.iterdir()}, {"report.json"})
        status, headers, report = self.request(path=result["downloads"]["report"])
        self.assertEqual(status, 200)
        self.assertEqual(report, result["report"])
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assert_error(self.request(path=f"/api/download/{result['analysis_id']}/gpx"), 404)

    def test_real_fixture_run(self):
        candidates = [web_app.PROJECT_DIR / "examples" / "34044834.gpx", web_app.PROJECT_DIR / "34044834.gpx"]
        fixture = next((p for p in candidates if p.is_file()), None)
        if fixture is None:
            self.skipTest("真实 GPX fixture 不存在；最小 GPX 端到端测试仍运行")
        shutil.copyfile(fixture, self.folder / "real.gpx")
        entry = next(x for x in self.files()[2]["files"] if x["name"] == "real.gpx")
        status, _, body = self.request("POST", "/api/run", {"file_id": entry["id"], "radius": 18})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["report"]["input_points"], len(body["original"]))
        self.assertEqual(body["report"]["output_points"], len(body["cleaned"]))
        self.assertGreater(body["report"]["input_length_m"], 10000)
        if body['candidate'] and body['candidate']['blocked_reasons']:
            self.assertEqual(body['cleaned'], body['original'])
            self.assertEqual(body['report']['removed_length_m'], 0)
            self.assertGreater(body['report']['proposed_output']['removed_length_m'], 0)
        else:
            self.assertGreater(body['report']['removed_length_m'], 0)

    def test_radius_validation_and_unknown_id(self):
        entry = self.files()[2]["files"][0]
        for radius in (True, None, "18", 0, -1, 1001):
            self.assert_error(self.request("POST", "/api/run", {"file_id": entry["id"], "radius": radius}), 400)
        self.assert_error(self.request("POST", "/api/run", {"file_id": str(self.folder / "路线.GPX")}), 404)
        self.assert_error(self.request("POST", "/api/run", {"file_id": 3}), 400)
        status, _, body = self.request("POST", "/api/run", {"file_id": entry["id"], "radius": 12.5})
        self.assertEqual(status, 200)
        self.assertEqual(body["report"]["config"]["match_radius_m"], 12.5)

    def test_bad_gpx_and_cleanup(self):
        before = set(self.output.iterdir()) if self.output.exists() else set()
        for data in ("not xml", "<gpx/>", MINIMAL_GPX.replace('lat="36.75"', 'lat="NaN"'),
                     MINIMAL_GPX.replace('lon="3.05"', 'lon="200"')):
            (self.folder / "路线.GPX").write_text(data, encoding="utf-8")
            self.assert_error(self.run_route(), 400)
        self.assertEqual(set(self.output.iterdir()), before)

    def test_reject_entity_declaration(self):
        attack = '<!DOCTYPE gpx [<!ENTITY test "attack">]>' + MINIMAL_GPX.split("?>", 1)[1]
        for encoding in ("utf-8", "utf-16", "utf-32"):
            (self.folder / "路线.GPX").write_bytes(attack.encode(encoding))
            self.assert_error(self.run_route(), 400)

    def test_size_limit_rechecked_after_listing(self):
        entry = self.files()[2]["files"][0]
        with (self.folder / "路线.GPX").open("wb") as file:
            file.truncate(web_app.MAX_GPX_BYTES + 1)
        self.assert_error(self.request("POST", "/api/run", {"file_id": entry["id"]}), 413)

    def test_missing_file_after_listing(self):
        entry = self.files()[2]["files"][0]
        (self.folder / "路线.GPX").unlink()
        self.assert_error(self.request("POST", "/api/run", {"file_id": entry["id"]}), 404)

    def test_json_and_content_type(self):
        for payload in (b"", b"{", b"[]", b"null", b"\xff", b'{"radius":NaN}', b"[" * 1500):
            self.assert_error(self.request("POST", "/api/run", raw=payload), 400)
        self.assert_error(self.request("POST", "/api/files", raw=b"x" * (web_app.MAX_BODY_BYTES + 1)), 413)
        self.assert_error(self.request("POST", "/api/files", raw=b"{}", headers={"Content-Type": "text/plain"}), 415)
        self.assert_error(self.request("POST", "/api/files", body={}, headers={"Transfer-Encoding": "chunked"}), 400)
        self.assert_error(self.request("POST", "/api/files", raw=b"{}", headers={"Content-Length": "-1"}), 400)
        self.assert_error(self.request("POST", "/api/files", raw=b"{}", headers={"Content-Length": "abc"}), 400)

    def test_early_rejection_delivers_json_and_graceful_eof(self):
        # Send the body only after rejection headers are available: this pins the
        # Windows unread-body close race without retries or weaker validation.
        for invalid_header, expected in ((b'Content-Type: text/plain\r\nContent-Length: 2', 415),
                                         (b'Content-Type: application/json\r\nContent-Length: -1', 400),
                                         (b'Content-Type: application/json\r\nContent-Length: 16385', 413)):
            with self.subTest(header=invalid_header):
                with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5) as connection:
                    request = (f'POST /api/files HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'.encode()
                               + invalid_header + b'\r\n\r\n')
                    connection.sendall(request)
                    received = b''
                    while b'\r\n\r\n' not in received:
                        chunk = connection.recv(4096)
                        self.assertTrue(chunk, 'response closed before JSON headers')
                        received += chunk
                    connection.sendall(b'{}')
                    connection.shutdown(socket.SHUT_WR)
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                    headers, body = received.split(b'\r\n\r\n', 1)
                    self.assertIn(f' {expected} '.encode(), headers.split(b'\r\n', 1)[0])
                    self.assertEqual(set(json.loads(body)), {'error'})

    def test_host_origin_and_cors(self):
        port = self.server.server_port
        good = f"http://127.0.0.1:{port}"
        self.assertEqual(self.files()[0], 200)  # No-Origin local scripts are accepted.
        status, headers, _ = self.request("POST", "/api/files", {"folder": str(self.folder)}, {"Origin": good})
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for origin in ("https://evil.example", "null", f"http://localhost:{port}", good + "/"):
            self.assert_error(self.request("POST", "/api/files", {}, {"Origin": origin}), 403)
        for host in ("evil.example", "127.0.0.1:1", f"127.0.0.1:{port}.evil.example"):
            self.assert_error(self.request(headers={"Host": host}), 403)
        self.assert_error(self.request(headers={"Origin": "https://evil.example"}), 403)
        self.assert_error(self.request(headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.assert_error(self.request("OPTIONS", "/api/files"), 405)
        status, _, _ = self.request(headers={"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})
        self.assertEqual(status, 200)

    def test_duplicate_headers(self):
        for name, value in (("Host", f"127.0.0.1:{self.server.server_port}"),
                            ("Origin", f"http://127.0.0.1:{self.server.server_port}"),
                            ("Content-Type", "application/json"), ("Content-Length", "2")):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
            try:
                connection.putrequest("POST", "/api/files", skip_host=True)
                headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json", "Content-Length": "2"}
                if name == "Origin":
                    headers["Origin"] = value
                for key, item in headers.items():
                    connection.putheader(key, item)
                connection.putheader(name, value)
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 403 if name in ("Host", "Origin") else 415 if name == "Content-Type" else 400)
                self.assertIn("error", json.loads(response.read()))
            finally:
                connection.close()

    def test_static_and_download_traversal(self):
        for path in ("/../README.md", "/%2e%2e/README.md", "/%2e%2e%2fREADME.md", "/..%5cREADME.md", "/%00", "/C:/Windows/win.ini",
                     "/api/download/../../README.md", "/api/download/%2e%2e/report", "/api/download/unknown/gpx", "/route_dedupe.py"):
            status, _, body = self.request(path=path)
            self.assertIn(status, (403, 404), (path, status, body))
        self.assert_error(self.request(path="/api/download/cleaned.gpx"), 404)
        self.assert_error(self.request(path="/api/unknown"), 404)
        self.assert_error(self.request("POST", "/api/unknown", {}), 404)
        self.assert_error(self.request("DELETE", "/api/config"), 405)

    def make_link(self, source, target, directory=False):
        try:
            target.symlink_to(source, target_is_directory=directory)
        except OSError:
            self.skipTest("当前 Windows 账户没有创建符号链接的权限")
        self.addCleanup(target.unlink)

    def test_symlink_files_and_directories(self):
        external = self.root / "outside.gpx"
        external.write_text(MINIMAL_GPX, encoding="utf-8")
        self.make_link(external, self.folder / "escape.gpx")
        self.make_link(self.web, self.folder / "linked-folder", directory=True)
        names = {entry["relative_path"] for entry in self.files(recursive=True)[2]["files"]}
        self.assertNotIn("escape.gpx", names)
        self.assertFalse(any(name.startswith("linked-folder/") for name in names))
        self.assert_error(self.request("POST", "/api/files", {"folder": str(self.folder / "linked-folder")}), 403)
        self.make_link(external, self.web / "escape.gpx")
        self.assert_error(self.request(path="/escape.gpx"), 403)

    def test_replaced_with_symlink_after_listing(self):
        entry = self.files()[2]["files"][0]
        target = self.folder / "路线.GPX"
        target.unlink()
        self.make_link(self.folder / "sub" / "nested.gpx", target)
        self.assert_error(self.request("POST", "/api/run", {"file_id": entry["id"]}), 403)

    def test_download_symlink_rejected(self):
        status, _, result = self.run_route()
        self.assertEqual(status, 200)
        target = self.output / result["analysis_id"] / "report.json"
        target.unlink()
        self.make_link(self.folder / "路线.GPX", target)
        self.assert_error(self.request(path=result["downloads"]["report"]), 403)

    def analyze_laps(self):
        import numpy as np
        from pyproj import Transformer
        angle = np.linspace(0, 4 * np.pi, 401)
        xy = np.column_stack((500000 + 200 * np.cos(angle), 4000000 + 200 * np.sin(angle)))
        inv = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)
        coords = np.column_stack(inv.transform(xy[:, 0], xy[:, 1]))
        web_app.route_dedupe.write_gpx(self.folder / '路线.GPX', coords, np.zeros(len(coords)), '测试重复圈')
        entry = self.files()[2]['files'][0]
        status, _, result = self.request('POST', '/api/analyze', {'file_id': entry['id']})
        self.assertEqual(status, 200, result)
        self.assertIsNotNone(result['candidate'])
        self.assertFalse(result['candidate']['blocked_reasons'])
        return result

    def decision(self, result, **changes):
        body = {'analysis_id': result['analysis_id'], 'plan_digest': result['plan_digest'],
                'goal': result['candidate']['goal'], 'confirm': True,
                'acknowledge_access_loss': False, 'acknowledge_geometry_only': True}
        body.update(changes)
        return self.request('POST', '/api/apply', body)

    def test_confirmed_apply_and_manifest(self):
        result = self.analyze_laps()
        source = (self.folder / '路线.GPX').read_bytes()
        status, _, exported = self.decision(result)
        self.assertEqual(status, 200, exported)
        self.assertTrue(exported['report']['confirmed'])
        self.assertFalse(exported['report']['road_identity_verified'])
        self.assertEqual(set(exported['downloads']), {'gpx', 'geojson', 'report', 'manifest'})
        for kind, url in exported['downloads'].items():
            status, headers, data = self.request(path=url)
            self.assertEqual(status, 200, (kind, data))
            self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual((self.folder / '路线.GPX').read_bytes(), source)
        out = self.output / exported['run_id']
        self.assertEqual({p.name for p in out.iterdir()}, set(web_app.DOWNLOAD_NAMES.values()))

    def test_apply_requires_exact_decisions(self):
        result = self.analyze_laps()
        for changes, code in (({'confirm': False}, 400), ({'confirm': 1}, 400),
                              ({'acknowledge_geometry_only': False}, 400),
                              ({'goal': 'main_loop'}, 400), ({'plan_digest': 'bad'}, 409)):
            with self.subTest(changes=changes):
                self.assert_error(self.decision(result, **changes), code)
        self.assert_error(self.request('POST', '/api/apply', {'analysis_id': 'missing'}), 409)
        self.assert_error(self.request('POST', '/api/apply', {}), 400)

    def test_tail_requires_access_loss_acknowledgment(self):
        import numpy as np
        from pyproj import Transformer
        points = np.asarray([[0, 0], [1000, 0], [1000, 300], [1600, 300],
                             [1600, -300], [1000, -300], [1000, 0], [0, 0]], dtype=float)
        points += [500000, 4000000]
        inv = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)
        coords = np.column_stack(inv.transform(points[:, 0], points[:, 1]))
        web_app.route_dedupe.write_gpx(self.folder / '路线.GPX', coords, np.zeros(len(coords)), '测试尾巴')
        entry = self.files()[2]['files'][0]
        status, _, result = self.request('POST', '/api/analyze', {'file_id': entry['id']})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['candidate']['goal'], 'main_loop')
        self.assertFalse(result['candidate']['blocked_reasons'])
        for acknowledgment in (False, None, 1, 'true'):
            self.assert_error(self.decision(result, acknowledge_access_loss=acknowledgment), 400)
        self.assertEqual(self.decision(result, acknowledge_access_loss=True)[0], 200)

    def test_failed_apply_does_not_publish_partial_artifacts(self):
        result = self.analyze_laps()
        before_dirs = set(self.output.iterdir())
        before_downloads = dict(self.server.downloads)
        def fail_after_write(path, out, *args, **kwargs):
            out.mkdir()
            (out / 'cleaned.gpx').write_text('partial', encoding='utf-8')
            raise web_app.route_plans.PlanError('Cannot publish output files')
        with mock.patch.object(web_app.route_plans, 'apply', side_effect=fail_after_write):
            self.assert_error(self.decision(result), 400)
        self.assertEqual(set(self.output.iterdir()), before_dirs)
        self.assertEqual(self.server.downloads, before_downloads)

    def test_apply_rejects_changed_source_and_expiry(self):
        result = self.analyze_laps()
        path = self.folder / '路线.GPX'
        path.write_bytes(path.read_bytes() + b'\n')
        self.assert_error(self.decision(result), 409)
        self.server.analyses[result['analysis_id']]['created'] -= web_app.ANALYSIS_TTL_SECONDS + 1
        self.assert_error(self.decision(result), 409)

    def test_apply_blocks_missing_or_unsafe_candidate(self):
        result = self.run_route()[2]
        self.assert_error(self.request('POST', '/api/apply', {'analysis_id': result['analysis_id']}), 409)
        result = self.analyze_laps()
        stored = self.server.analyses[result['analysis_id']]['analysis']
        stored['candidate']['blocked_reasons'].append('测试：禁止转换')
        self.assert_error(self.decision(result), 409)

    def test_admission_precedes_source_read(self):
        entry = self.files()[2]['files'][0]
        self.server.run_lock.acquire()
        try:
            with mock.patch.object(self.server, 'read_source', side_effect=AssertionError('must not read')):
                self.assert_error(self.request('POST', '/api/analyze', {'file_id': entry['id']}), 429)
        finally:
            self.server.run_lock.release()

    def test_analysis_retention_is_bounded(self):
        entry = self.files()[2]['files'][0]
        with mock.patch.object(web_app, 'MAX_ANALYSES', 2):
            for _ in range(3):
                self.assertEqual(self.request('POST', '/api/analyze', {'file_id': entry['id']})[0], 200)
            self.assertLessEqual(len(self.server.analyses), 2)

    def test_classification_matches_gpx_classifier_and_publishes_only_report(self):
        entry = self.files()[2]['files'][0]
        source = self.folder / '路线.GPX'
        original = source.read_bytes()
        expected = web_app.loop_recognition.classify_route_bytes(original)
        status, _, result = self.request('POST', '/api/classify', {'file_id': entry['id']})
        self.assertEqual(status, 200, result)
        self.assertEqual(set(result['downloads']), {'report'})
        self.assertEqual(self.request(path=result['downloads']['report'])[2], expected)
        self.assertEqual({k: result[k] for k in expected}, expected)
        self.assertEqual({p.name for p in (self.output / result['classification_id']).iterdir()}, {'report.json'})
        self.assert_error(self.request('POST', '/api/apply', {'analysis_id': result['classification_id']}), 409)
        self.assert_error(self.request('POST', '/api/recognize-loops', {'file_id': entry['id']}), 404)
        self.assertEqual(source.read_bytes(), original)

    def test_classification_matches_toproute_classifier(self):
        route = {"topRouteId": "test-route", "segments": [{"routePoints": [
            {"longitude": 3.05, "latitude": 36.75},
            {"longitude": 3.06, "latitude": 36.75},
            {"longitude": 3.06, "latitude": 36.76},
            {"longitude": 3.05, "latitude": 36.76},
            {"longitude": 3.05, "latitude": 36.75},
        ]}]}
        source = self.folder / "routes.json"
        source.write_text(json.dumps(route), encoding="utf-8")
        status, _, listing = self.request("POST", "/api/files", {"paths": [str(source)]})
        self.assertEqual(status, 200, listing)
        entry = listing["files"][0]
        status, _, result = self.request("POST", "/api/classify", {"file_id": entry["id"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["category"], 2)
        self.assertEqual(self.request(path=result["downloads"]["report"])[2],
                         web_app.loop_recognition.classify_export_record(route))

    def test_route_label_crud_and_identity_after_rescan(self):
        source = self.folder / "路线.GPX"
        original = source.read_bytes()
        scan = {"paths": [str(source)]}
        endpoint = "/api/route-labels"
        entry = self.request("POST", "/api/files", scan)[2]["files"][0]
        self.assert_error(self.request("POST", endpoint, {"file_id": entry["id"], "code": "4-6"}), 400)
        self.assert_error(self.request("POST", endpoint, {"file_id": entry["id"], "code": "3-2"}), 400)
        status, _, saved = self.request("POST", endpoint, {"file_id": entry["id"], "code": "3-4"})
        self.assertEqual(status, 200, saved)
        key = entry["label_key"]
        self.assertEqual(saved["label"]["key"], key)
        rescanned = self.request("POST", "/api/files", scan)[2]["files"][0]
        self.assertNotEqual(rescanned["id"], entry["id"])
        self.assertEqual(rescanned["label_key"], key)
        with web_app.create_server(port=0, output_dir=self.output) as reopened:
            self.assertIn(saved["label"], reopened.list_labels()["labels"])
        status, _, updated = self.request("POST", endpoint, {"key": key, "code": "3-5"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["label"]["code"], "3-5")
        self.assertEqual(self.request("POST", endpoint, {"key": key, "code": None})[2],
                         {"key": key, "label": None})
        self.assertNotIn(key, {label["key"] for label in self.request(path=endpoint)[2]["labels"]})
        self.assertEqual(source.read_bytes(), original)

    def test_old_labels_preserve_or_merge_their_meaning_after_upgrade(self):
        output = self.folder / "label-output"
        output.mkdir()
        migrations = {"2-2": "legacy-2-2", "4-1": "4-1", "4-2": "legacy-4-2",
                      "4-3": "legacy-4-3", "4-4": "4-2", "4-5": "4-3"}
        keys = {code: str(index) * 64 for index, code in enumerate(migrations)}
        db = sqlite3.connect(output / "route-labels.sqlite3")
        try:
            db.execute("CREATE TABLE route_labels (key TEXT PRIMARY KEY, name TEXT, source TEXT, code TEXT)")
            db.executemany("INSERT INTO route_labels VALUES (?, ?, ?, ?)",
                           ((key, code, "source", code) for code, key in keys.items()))
            db.commit()
        finally:
            db.close()
        with web_app.create_server(port=0, output_dir=output) as server:
            self.assertEqual({item["name"]: item["code"] for item in server.list_labels()["labels"]},
                             migrations)
            server.save_label({"key": keys["4-2"], "code": "2-4"})
            migrations["4-2"] = "2-4"
            self.assertEqual({item["name"]: item["code"] for item in server.list_labels()["labels"]},
                             migrations)

    def test_source_read_honors_smaller_limit_when_file_grows_after_stat(self):
        entry = self.files()[2]['files'][0]
        record = self.server.source_record(entry['id'])
        real_stat = record.path.stat()
        stale_stat = mock.Mock(st_dev=real_stat.st_dev, st_ino=real_stat.st_ino, st_size=1)
        # Actual source exceeds this lower limit although the admitted stat does not.
        with mock.patch.object(web_app, 'safe_file', return_value=stale_stat):
            with self.assertRaises(web_app.APIError) as caught:
                self.server.read_source(record, max_bytes=32)
        self.assertEqual(caught.exception.status, 413)

    def test_bind_and_cli_reject_nonloopback(self):
        for host in ("0.0.0.0", "192.168.1.1", "example.com", "::", "::1"):
            with self.assertRaises(ValueError):
                web_app.create_server(host=host, port=0)
        self.assertEqual(web_app.loopback_host("localhost"), "127.0.0.1")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with mock.patch("sys.stderr"):
            for argv in (["--host", "0.0.0.0"], ["--port", "-1"], ["--port", "65536"]):
                with self.assertRaises(SystemExit):
                    web_app.main(argv)


if __name__ == "__main__":
    unittest.main()
