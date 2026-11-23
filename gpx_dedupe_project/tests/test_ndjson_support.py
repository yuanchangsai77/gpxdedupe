"""NDJSON file discovery and large streamed export regression checks."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop_recognition as lr
import web_app


def record(identifier):
    return {'_id': {'$oid': identifier}, 'topRouteId': identifier,
            'segments': [{'routePoints': [
                {'longitude': 103.8 + i * .001, 'latitude': 30.2 + i * .001}
                for i in range(3)]}]}


class NDJSONSupportTests(unittest.TestCase):
    def test_discovery_and_selection_for_direct_and_directory_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = object.__new__(web_app.LocalServer)
            server.files = {}
            server.state_lock = threading.Lock()
            for suffix in ('.ndjson', '.jsonl', '.export.json'):
                path = root / ('routes' + suffix)
                path.write_text('\n'.join(json.dumps(record(str(i))) for i in range(2)), encoding='utf-8')
                listing = server.list_files({'paths': [str(path)]})
                self.assertEqual(listing['errors'], [])
                self.assertEqual(len(listing['files']), 2)
                selected = server.source_record(listing['files'][1]['id'])
                self.assertEqual(selected.export.load(selected.entry)['topRouteId'], '1')
            listing = server.list_files({'paths': [str(root)]})
            self.assertEqual(listing['errors'], [])
            self.assertEqual(len(listing['files']), 6)

    def test_large_export_limits_and_seekable_loading(self):
        self.assertGreaterEqual(lr.MAX_EXPORT_BYTES, 3291147278)
        self.assertGreaterEqual(lr.MAX_EXPORT_RECORDS, 42130)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routes.ndjson'
            path.write_text('\n'.join(json.dumps(record(str(i))) for i in range(3)), encoding='utf-8')
            index = lr.RouteExportIndex(path)
            self.assertIsNone(index._document_records)
            self.assertEqual(index.load(index.entries[-1])['topRouteId'], '2')
            index.verify(full_hash=True)
            with patch.object(lr, 'MAX_EXPORT_BYTES', 64):
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path)
            with patch.object(lr, 'MAX_EXPORT_RECORDS', 2):
                with self.assertRaises(lr.ExportError):
                    lr.RouteExportIndex(path)
            path.write_text(path.read_text() + '\n', encoding='utf-8')
            with self.assertRaises(lr.ExportError):
                index.load(index.entries[0])


if __name__ == '__main__':
    unittest.main()
