"""Prove recognition works when its script is the only copied project file."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop_recognition as lr
from benchmark_loops import gpx_bytes, synthetic_cases


class StandaloneRecognitionTests(unittest.TestCase):
    def run_python(self, directory, *args, input_text=None):
        env = os.environ.copy()
        # Neither the repository nor the source script directory may leak into
        # Python's search path through inherited shell configuration.
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-X', 'utf8', *map(str, args)], cwd=directory,
            env=env, input=input_text, capture_output=True, encoding='utf-8', timeout=60)
        return result

    def copy_script(self, directory):
        script = Path(directory) / 'loop_recognition.py'
        shutil.copyfile(Path(lr.__file__), script)
        return script

    def test_copied_script_api_matches_every_synthetic_case(self):
        with tempfile.TemporaryDirectory(prefix='gpx-standalone-') as directory:
            script = self.copy_script(directory)
            source = Path(directory) / 'input.gpx'
            # One process verifies every fixture through the public bytes API;
            # a separate invocation below drives the actual CLI directly.
            fixtures = []
            for case in synthetic_cases():
                raw, _ = gpx_bytes(case.points)
                fixtures.append(raw.decode('utf-8'))
            source.write_text(json.dumps(fixtures), encoding='utf-8')
            bootstrap = '''
import importlib.abc, json, runpy, sys
from pathlib import Path
class BlockProjectImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'route_dedupe', 'route_plans', 'web_app', 'gpx_dedupe_project', 'matplotlib'}:
            raise RuntimeError('Unexpected project/plotting dependency: ' + fullname)
sys.meta_path.insert(0, BlockProjectImports())
namespace = runpy.run_path(sys.argv[1], run_name='standalone_recognition')
fixtures = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
results = [namespace['recognize_bytes'](raw.encode('utf-8'), source_name='input.gpx') for raw in fixtures]
print(json.dumps(results, allow_nan=False))
'''
            result = self.run_python(directory, '-c', bootstrap, script, source)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            expected = [lr.recognize_bytes(raw.encode('utf-8'), source_name='input.gpx') for raw in fixtures]
            self.assertEqual(actual, expected)
            self.assertEqual({path.name for path in Path(directory).iterdir()},
                             {'loop_recognition.py', 'input.gpx'})

    def test_isolated_cli_stdout_report_and_overwrite_protection(self):
        case = next(case for case in synthetic_cases() if case.category == 'internal_branch')
        raw, _ = gpx_bytes(case.points)
        before = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory(prefix='gpx-standalone-cli-') as directory:
            script = self.copy_script(directory)
            source = Path(directory) / '路线.gpx'
            output = Path(directory) / '报告.json'
            source.write_bytes(raw)
            expected = lr.recognize_bytes(raw, source_name=source.name)
            result = self.run_python(directory, script, source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {script.name, source.name})
            result = self.run_python(directory, script, source, '--out', output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), expected)
            saved = output.read_bytes()
            again = self.run_python(directory, script, source, '--out', output)
            self.assertEqual(again.returncode, 2)
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual({p.name for p in Path(directory).iterdir()},
                             {script.name, source.name, output.name})

    def test_isolated_cli_errors_never_write_cleaned_files(self):
        with tempfile.TemporaryDirectory(prefix='gpx-standalone-errors-') as directory:
            script = self.copy_script(directory)
            source = Path(directory) / 'bad.gpx'
            output = Path(directory) / 'report.json'
            source.write_bytes(b'<gpx/>')
            for args in ((source,), (Path(directory)/'missing.gpx',),
                         (source, '--radius', 'nan')):
                result = self.run_python(directory, script, *args, '--out', output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(output.exists())
            help_result = self.run_python(directory, script, '--help')
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {script.name, source.name})

    def test_copied_cli_classifies_without_project_or_label_files(self):
        cases = list(synthetic_cases())
        names = ['single_loop', 'open_route', 'figure_eight']
        with tempfile.TemporaryDirectory(prefix='gpx-classify-cli-') as directory:
            script = self.copy_script(directory)
            source = Path(directory) / '整体路线.gpx'
            output = Path(directory) / '整体类别.json'
            for name in names:
                case = next(case for case in cases if case.category == name)
                raw, _ = gpx_bytes(case.points)
                source.write_bytes(raw)
                expected = lr.classify_route_bytes(raw)
                result = self.run_python(directory, script, source, '--classify')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(source.read_bytes(), raw)
            source.write_bytes(b'invalid GPX')
            result = self.run_python(directory, script, source, '--classify', '--out', output)
            self.assertEqual(result.returncode, 2)
            failure = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(failure['category'], 1)
            self.assertEqual(failure['reason_code'], 'invalid_input')
            saved = output.read_bytes()
            again = self.run_python(directory, script, source, '--classify', '--out', output)
            self.assertEqual(again.returncode, 2)
            self.assertEqual(output.read_bytes(), saved)
            result = self.run_python(directory, script, Path(directory)/'missing.gpx', '--classify')
            self.assertEqual(json.loads(result.stdout)['category'], 1)
            self.assertEqual(result.returncode, 2)
            self.assertEqual({p.name for p in Path(directory).iterdir()},
                             {script.name, source.name, output.name})

    def test_copied_script_reads_json_by_id_range_and_all_with_confirmation(self):
        from test_json_route_batch import route
        records = [route(f'{i:03d}')[0] for i in range(1,5)]
        with tempfile.TemporaryDirectory(prefix='gpx-json-isolation-') as directory:
            script = self.copy_script(directory)
            source = Path(directory)/'多路线.export.json'
            source.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
            before = source.read_bytes()
            single = self.run_python(directory, script, source, '--top-route-id', '002')
            self.assertEqual(single.returncode, 0, single.stderr)
            data = json.loads(single.stdout)
            self.assertEqual(data, [{'topRouteId': '002', 'category': 2}])
            self.assertNotIn('Yes/No', single.stderr)
            out = Path(directory)/'result.json'
            no = self.run_python(directory, script, source, '--start', '2', '--count', '2',
                                 '--out', out, input_text='No\n')
            self.assertEqual(no.returncode, 3, no.stderr)
            self.assertEqual(no.stdout, '')
            self.assertFalse(out.exists())
            self.assertIn('Yes/No', no.stderr)
            yes = self.run_python(directory, script, source, '--start', '2', '--count', '2', input_text='Yes\n')
            self.assertEqual(yes.returncode, 0, yes.stderr)
            data = json.loads(yes.stdout)
            self.assertEqual(data, [{'topRouteId': '002', 'category': 2},
                                    {'topRouteId': '003', 'category': 2}])
            self.assertIn('2/2', yes.stderr)
            self.assertNotIn('\r', yes.stderr)
            all_result = self.run_python(directory, script, source, '--all', input_text='Yes\n')
            self.assertEqual(all_result.returncode, 0, all_result.stderr)
            self.assertEqual(len(json.loads(all_result.stdout)),4)
            detailed = self.run_python(directory, script, source, '--top-route-id', '002', '--detailed')
            self.assertEqual(detailed.returncode, 0, detailed.stderr)
            self.assertEqual(json.loads(detailed.stdout)['processed_count'], 1)
            eof = self.run_python(directory, script, source, '--all', input_text='')
            self.assertEqual(eof.returncode, 3)
            self.assertEqual(eof.stdout, '')
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {source.name, script.name})

    def test_json_cli_output_preflight_and_invalid_selection_do_not_run_pilots(self):
        from test_json_route_batch import route
        with tempfile.TemporaryDirectory(prefix='gpx-json-cli-errors-') as directory:
            script = self.copy_script(directory)
            source = Path(directory)/'routes.json'
            source.write_text(json.dumps([route('001')[0],route('002')[0]]))
            out = Path(directory)/'exists.json'
            out.write_text('original')
            result = self.run_python(directory, script, source, '--all', '--out', out, input_text='Yes\n')
            self.assertEqual(result.returncode,2)
            self.assertNotIn('试算',result.stderr)
            self.assertEqual(out.read_text(),'original')
            result = self.run_python(directory, script, source, '--start', '1', input_text='Yes\n')
            self.assertEqual(result.returncode,2)
            self.assertNotIn('试算',result.stderr)
            result = self.run_python(directory, script, source, '--all', '--radius','12',input_text='Yes\n')
            self.assertEqual(result.returncode,2)
            self.assertNotIn('试算',result.stderr)

    def test_auto_format_does_not_decode_a_truncated_utf8_prefix(self):
        from test_json_route_batch import route
        with tempfile.TemporaryDirectory(prefix='gpx-json-utf8-prefix-') as directory:
            script = self.copy_script(directory)
            source = Path(directory)/'export.data'
            prefix = b'{"description":"' + b'a' * (4095-len(b'{"description":"'))
            record = route('001')[0]
            record.pop('description')
            payload = json.dumps(record, ensure_ascii=False).encode('utf-8')
            source.write_bytes(prefix + '中'.encode('utf-8') + b'",' + payload[1:])
            self.assertEqual(source.read_bytes()[4095:4098], '中'.encode('utf-8'))
            result = self.run_python(directory, script, source, '--top-route-id', '001')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [{'topRouteId':'001','category':2}])

    def test_package_import_needs_no_legacy_or_plotting_modules(self):
        # Public package-style imports used by Python clients must stay valid,
        # not only the standalone CLI path used by the copied-file tests.
        root = Path(__file__).resolve().parents[2]
        code = '''
import importlib.abc, sys
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[-1] in {'route_dedupe', 'route_plans', 'web_app', 'matplotlib'}:
            raise RuntimeError('Unexpected dependency: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
sys.path.insert(0, sys.argv[1])
from gpx_dedupe_project.loop_recognition import recognize_loops, recognize_bytes
assert callable(recognize_loops) and callable(recognize_bytes)
'''
        with tempfile.TemporaryDirectory(prefix='gpx-package-import-') as directory:
            result = self.run_python(directory, '-c', code, root)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
