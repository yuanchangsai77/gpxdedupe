"""Export adapter, source indexing and confirmation-first batch contract tests."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import loop_recognition as lr
from benchmark_loops import gpx_bytes, circle


def route(identifier='001'):
    raw, coords = gpx_bytes(circle())
    return {'topRouteId': identifier, 'username': 'PRIVATE USER', 'description': 'PRIVATE DESCRIPTION',
            'segments': [{'routePoints': [{'longitude': float(lon), 'latitude': float(lat), 'altitude': 100.}
                                         for lon, lat in coords]}]}, raw


class ExportBatchTests(unittest.TestCase):
    def write_records(self, directory, records, kind='ndjson', bom=False):
        path = Path(directory)/'export.json'
        if kind == 'ndjson':
            text = '\r\n'.join(json.dumps(r, ensure_ascii=False) for r in records) + '\r\n'
        elif kind == 'json_array':
            text = json.dumps(records, indent=2)
        else:
            text = json.dumps(records[0], indent=2)
        path.write_text(text, encoding='utf-8-sig' if bom else 'utf-8')
        return path

    def test_adapter_and_coordinate_api_match_gpx_without_transcoding(self):
        record, raw = route()
        actual = lr.classify_export_record(record)
        expected = lr.classify_route_bytes(raw)
        expected.pop('input_sha256')
        self.assertEqual(actual, expected)
        coords = [[p['longitude'], p['latitude']] for p in record['segments'][0]['routePoints']]
        self.assertEqual(lr.classify_coordinates(coords), actual)

    def test_adapter_validates_each_geometry_without_bridging(self):
        record, _ = route()
        for malformed in (None, {}, {'segments': []}, {'segments': [None]},
                          {'segments': [{'routePoints': []}]},
                          {'segments': record['segments']*2}):
            self.assertEqual(lr.classify_export_record(malformed)['category'], 1)
        for key, value in (('longitude', True), ('latitude', '1'), ('altitude', float('nan')),
                           ('longitude', 200.), ('latitude', float('inf'))):
            malformed = json.loads(json.dumps(record))
            malformed['segments'][0]['routePoints'][0][key] = value
            self.assertEqual(lr.classify_export_record(malformed)['category'], 1)

    def test_formats_bom_crlf_blank_lines_and_source_order(self):
        for kind in ('ndjson', 'json_array', 'json_object'):
            for bom in (False, True):
                with self.subTest(kind=kind, bom=bom), tempfile.TemporaryDirectory() as directory:
                    records = [route('03')[0], route('01')[0]]
                    path = self.write_records(directory, records, kind, bom)
                    if kind == 'ndjson':
                        data = path.read_bytes()
                        prefix = b'\xef\xbb\xbf' if bom else b''
                        path.write_bytes(prefix + b'\r\n' + data[len(prefix):])
                    index = lr.RouteExportIndex(path)
                    expected_count = 1 if kind == 'json_object' else 2
                    self.assertEqual(len(index.entries), expected_count)
                    self.assertEqual(index.load(index.entries[0]), records[0])
                    self.assertEqual(index.format, kind)
                    if kind == 'ndjson':
                        self.assertEqual(index.entries[0]['source_line'], 2)
                    index.verify(full_hash=True)

    def test_selection_start_count_id_all_and_duplicate_ids(self):
        records = [route(f'{i:03d}')[0] for i in range(1, 33)]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_records(directory, records)
            index = lr.RouteExportIndex(path)
            selected = index.select(start=10, count=20)
            self.assertEqual([e['source_index'] for e in selected], list(range(10,30)))
            self.assertEqual(index.select(top_route_id='001')[0]['source_index'], 1)
            self.assertEqual(len(index.select(all_routes=True)), 32)
            for args in ({}, {'start': 0, 'count': 2}, {'start': 1}, {'count': 1},
                         {'start': 30, 'count': 4}, {'start': True, 'count': 1},
                         {'top_route_id': '1'}, {'top_route_id': '001', 'all_routes': True}):
                with self.assertRaises(lr.ExportError):
                    index.select(**args)
        with tempfile.TemporaryDirectory() as directory:
            records = [route('same')[0], route('same')[0], {'segments': []}]
            index = lr.RouteExportIndex(self.write_records(directory, records))
            with self.assertRaises(lr.ExportError):
                index.select(top_route_id='same')
            self.assertEqual(len(index.select(all_routes=True)), 3)
            self.assertIn('duplicate_topRouteId', index.entries[0]['warnings'])
            self.assertIn('missing_or_invalid_topRouteId', index.entries[2]['warnings'])

    def test_outer_errors_and_size_limits_reject_before_classification(self):
        for text in ('', '{bad}', '{"x":1,"x":2}', '{"x":NaN}',
                     '{"x":Infinity}', '{"topRouteId":"x"}\nnot json', '['*2000):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'bad.json'
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_records(directory, [route()[0]])
            with patch.object(lr, 'MAX_EXPORT_RECORD_BYTES', 64):
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path, 'ndjson')
            with patch.object(lr, 'MAX_EXPORT_BYTES', 64):
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path)
            with patch.object(lr, 'MAX_EXPORT_RECORDS', 1):
                path = self.write_records(directory, [route('a')[0], route('b')[0]])
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path)

    def test_snapshot_mutation_rejected_for_ndjson_and_array(self):
        for kind in ('ndjson', 'json_array'):
            with tempfile.TemporaryDirectory() as directory:
                path = self.write_records(directory, [route()[0]], kind)
                index = lr.RouteExportIndex(path)
                path.write_bytes(path.read_bytes()+b' ')
                with self.assertRaises(lr.ExportError):
                    index.load(index.entries[0])
                with self.assertRaises(lr.ExportError):
                    index.verify(full_hash=True)

    def test_no_blank_and_eof_only_run_bounded_pilots(self):
        for answer in ('No\n', '\n', ''):
            with tempfile.TemporaryDirectory() as directory:
                index = lr.RouteExportIndex(self.write_records(directory, [route(str(i))[0] for i in range(6)]))
                calls = []
                def worker(record, cfg, timeout, **kwargs):
                    calls.append((record['topRouteId'], timeout))
                    return lr.classify_export_record(record, cfg)
                progress = io.StringIO()
                with self.assertRaises(lr.BatchCancelled):
                    lr.run_export_batch(index, index.entries, input_stream=io.StringIO(answer),
                                        progress=progress, worker=worker)
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(timeout <= 5 for _, timeout in calls))
                self.assertIn('Yes/No', progress.getvalue())
                self.assertIn('已取消', progress.getvalue())

    def test_yes_reuses_pilots_and_retains_order_progress_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            index = lr.RouteExportIndex(self.write_records(directory, [route(str(i))[0] for i in range(5)]))
            calls = []
            def worker(record, cfg, timeout, **kwargs):
                calls.append(record['topRouteId'])
                return lr.classify_export_record(record, cfg)
            progress = io.StringIO()
            result = lr.run_export_batch(index, index.entries, input_stream=io.StringIO('invalid\nYes\n'),
                                         progress=progress, worker=worker)
            self.assertEqual(len(calls), 5)
            self.assertEqual([r['topRouteId'] for r in result['results']], list(map(str,range(5))))
            self.assertEqual(result['processed_count'], 5)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('\r', progress.getvalue())
            self.assertIn('5/5', progress.getvalue())

    def test_completed_geometry_rejection_can_estimate_but_schema_errors_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            index = lr.RouteExportIndex(self.write_records(directory, [route(str(i))[0] for i in range(4)]))
            for code, metrics, expected in (('ambiguous_shape', {'graph_edges': 100}, '预计剩余约'),
                                             ('invalid_input', {}, '无法可靠估计')):
                progress = io.StringIO()
                def worker(record, cfg, timeout, **kwargs):
                    return lr._classification_result(1, ['test'], cfg, metrics, reason_code=code)
                with self.assertRaises(lr.BatchCancelled):
                    lr.run_export_batch(index, index.entries, input_stream=io.StringIO('No\n'),
                                        progress=progress, worker=worker)
                self.assertIn(expected, progress.getvalue())

    def test_worker_launch_failure_is_not_a_geometric_conclusion(self):
        with patch.object(lr.subprocess, 'Popen', side_effect=OSError('private path')):
            result = lr._run_route_worker(route()[0], lr.RouteShapeConfig(), 60)
        self.assertEqual(result['reason_code'], 'internal_error')
        self.assertNotIn('private path', json.dumps(result))

    def test_non_tty_heartbeat_and_confirmation_wait_not_in_eta(self):
        with tempfile.TemporaryDirectory() as directory:
            index = lr.RouteExportIndex(self.write_records(directory, [route('one')[0]]))
            now = [0.]
            def worker(record, cfg, timeout, heartbeat=None):
                now[0] = 6.
                heartbeat()
                return lr.classify_export_record(record, cfg)
            progress = io.StringIO()
            result = lr.run_export_batch(index, index.entries, progress=progress, worker=worker,
                                         clock=lambda: now[0])
            self.assertIn('心跳', progress.getvalue())
            self.assertNotIn('\r', progress.getvalue())
            self.assertEqual(result['elapsed_seconds'], 6.)

    def test_two_routes_only_one_pilot_and_single_has_no_prompt(self):
        for count in (1,2):
            with tempfile.TemporaryDirectory() as directory:
                index = lr.RouteExportIndex(self.write_records(directory, [route(str(i))[0] for i in range(count)]))
                calls, progress = [], io.StringIO()
                def worker(record, cfg, timeout, **kwargs):
                    calls.append(timeout)
                    return lr.classify_export_record(record, cfg)
                result = lr.run_export_batch(index, index.entries, input_stream=io.StringIO('Yes\n'),
                                             progress=progress, worker=worker)
                self.assertEqual(len(calls), count)
                if count == 1:
                    self.assertNotIn('Yes/No', progress.getvalue())
                else:
                    self.assertEqual(sum(t <= 5 for t in calls),1)
                self.assertEqual(result['status'], 'complete')

    def test_individual_geometry_failure_does_not_drop_a_route(self):
        with tempfile.TemporaryDirectory() as directory:
            index = lr.RouteExportIndex(self.write_records(directory, [route('ok')[0], {'topRouteId':'bad','segments':[]}]))
            result = lr.run_export_batch(index, index.entries, input_stream=io.StringIO('Yes\n'),
                                         progress=io.StringIO(), worker=lambda r,c,t,**kw:lr.classify_export_record(r,c))
            self.assertEqual(result['category_counts']['1'],1)
            self.assertEqual(result['processed_count'],2)

    def test_timeout_worker_is_killed_and_reaped_and_keyboard_interrupt_is_not_swallowed(self):
        class TimedOut:
            returncode = None
            killed = False
            def communicate(self, **kwargs):
                if not self.killed:
                    raise lr.subprocess.TimeoutExpired('worker', .1)
                self.returncode = -1
                return b'', b''
            def kill(self): self.killed = True
            def poll(self): return self.returncode
        process = TimedOut()
        with patch.object(lr.subprocess, 'Popen', return_value=process), \
                patch.object(lr.time, 'monotonic', side_effect=[0., 0., 1.]):
            result = lr._run_route_worker(route()[0], lr.RouteShapeConfig(), .1)
        self.assertEqual(result['reason_code'], 'resource_limit')
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.returncode)
        class Interrupted(TimedOut):
            def communicate(self, **kwargs):
                if not self.killed: raise KeyboardInterrupt()
                self.returncode=-1
                return b'',b''
        process = Interrupted()
        with patch.object(lr.subprocess, 'Popen', return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                lr._run_route_worker(route()[0], lr.RouteShapeConfig(), 60)
        self.assertTrue(process.killed)

    def test_worker_transport_is_seekable_minimal_and_not_ascii_expanded(self):
        record, _ = route()
        record['segments'][0]['description'] = '私' * 100000
        for point in record['segments'][0]['routePoints']:
            point['annotation'] = 'PRIVATE ANNOTATION'
        received = []
        def execute(request_file, cfg, timeout, heartbeat):
            self.assertTrue(request_file.seekable())
            payload = request_file.read()
            received.append(payload)
            request = lr._strict_json(payload)
            self.assertEqual(set(request['record']), {'segments'})
            self.assertEqual(set(request['record']['segments'][0]), {'routePoints'})
            self.assertEqual(set(request['record']['segments'][0]['routePoints'][0]),
                             {'longitude', 'latitude', 'altitude'})
            return lr.classify_export_record(request['record'], cfg)
        with patch.object(lr, '_execute_route_worker', side_effect=execute), \
                patch.object(lr, 'MAX_EXPORT_RECORD_BYTES', 20000):
            result = lr._run_route_worker(record, lr.RouteShapeConfig(), 60)
        self.assertEqual(result['category'], 2)
        self.assertLess(len(received[0]), 20000)
        self.assertNotIn(b'PRIVATE', received[0])

    def test_real_stalled_worker_timeout_does_not_write_to_stdin_pipe(self):
        # Simulate a worker blocked before reading its request. A request much
        # larger than a Windows pipe buffer must not block the deadline loop.
        record, _ = route()
        record['segments'][0]['routePoints'] *= 30
        real_popen = lr.subprocess.Popen
        processes, heartbeats = [], []
        def stalled(command, **kwargs):
            self.assertNotEqual(kwargs['stdin'], lr.subprocess.PIPE)
            self.assertTrue(kwargs['stdin'].seekable())
            process = real_popen([sys.executable, '-I', '-B', '-c',
                                 'import threading; threading.Event().wait(60)'], **kwargs)
            processes.append(process)
            return process
        with patch.object(lr.subprocess, 'Popen', side_effect=stalled):
            result = lr._run_route_worker(record, lr.RouteShapeConfig(), .4,
                                          heartbeat=lambda: heartbeats.append(True))
        self.assertEqual(result['reason_code'], 'resource_limit')
        self.assertTrue(heartbeats)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_failed_publication_never_deletes_a_concurrent_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'result.json'
            def fail_write(result, stream, **kwargs):
                output.write_text('OTHER PROCESS', encoding='utf-8')
                raise OSError('failed writing temporary report')
            with patch.object(lr.json, 'dump', side_effect=fail_write):
                with self.assertRaises(OSError):
                    lr._publish_json({'x':1}, output)
            self.assertEqual(output.read_text(), 'OTHER PROCESS')
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {'result.json'})

    def test_output_preflight_and_exclusive_publish_protect_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.json'
            source.write_text('original')
            with self.assertRaises(lr.ExportError): lr._preflight_output(source,source)
            out = Path(directory)/'result.json'
            lr._preflight_output(out,source)
            out.write_text('created concurrently')
            with self.assertRaises(FileExistsError): lr._publish_json({'x':1},out)
            self.assertEqual(out.read_text(),'created concurrently')
            new = Path(directory)/'new.json'
            with patch.object(lr.json, 'dump', side_effect=OSError('write failed')):
                with self.assertRaises(OSError): lr._publish_json({'x':1},new)
            self.assertFalse(new.exists())


if __name__ == '__main__':
    unittest.main()
