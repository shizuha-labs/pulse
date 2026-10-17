"""Offline execution of the actual shell canary; no live credentials or HTTP."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'k3s/scripts/attachment_canary.sh'
FAKE_CURL = r'''#!/usr/bin/env -S python3 -I
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FIXTURE'])
args = sys.argv[1:]
assert 'Authorization: Bearer synthetic-only' in args
assert '-L' not in args and '--location' not in args
count = root / 'count'
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
config = json.loads((root / 'config').read_text())
uid = '12345678-1234-4234-8234-123456789abc'
row = {'id': uid, 'storage_key': 'synthetic/key'}
if n == 1:
    assert '-X' in args and args[args.index('-X') + 1] == 'POST'
    file_arg = next(a for a in args if a.startswith('file=@'))
    (root / 'payload').write_bytes(pathlib.Path(file_arg[6:]).read_bytes())
if n == 4:
    assert args[-1].endswith('/' + uid + '/serve/')
if n == 5:
    assert args[args.index('-X') + 1] == 'DELETE'
body = {1: json.dumps(row), 2: json.dumps([row]), 3: json.dumps(row),
        4: '', 5: '', 6: '[]'}[n].encode()
if n == 4:
    body = (root / 'payload').read_bytes()
code = {1: '201', 5: '204'}.get(n, '200')
if n == config.get('step', 6):
    code = str(config.get('code', code))
    if 'body' in config:
        body = config['body'].encode()
    if config.get('network'):
        sys.exit(7)
pathlib.Path(args[args.index('-o') + 1]).write_bytes(body)
print(code, end='')
'''


class CanaryScriptTests(unittest.TestCase):
    def run_case(self, config, success=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / '.shizuha/auth'
            auth.mkdir(parents=True)
            (auth / 'token-san.json').write_text(json.dumps({'accessToken': 'synthetic-only'}))
            (root / 'config').write_text(json.dumps(config))
            curl = root / 'curl'
            curl.write_text(FAKE_CURL)
            curl.chmod(0o700)
            env = dict(os.environ, HOME=tmp, FIXTURE=tmp,
                       PATH=tmp + os.pathsep + os.environ['PATH'])
            # Canary's isolated Python must keep verification active even here.
            env['PYTHONOPTIMIZE'] = '2'
            result = subprocess.run(['bash', str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            self.assertEqual('CANARY PASS' in result.stdout, success)
            if success:
                self.assertEqual((root / 'count').read_text(), '6')

    def test_success_list_and_paginated_list(self):
        self.run_case({}, success=True)
        self.run_case({'body': '{"count":0,"next":null,"results":[]}'}, success=True)

    def test_cleanup_http_and_shape_fail_closed(self):
        for code in [401, 403, 500, 302]:
            with self.subTest(code=code):
                self.run_case({'code': code, 'body': '{"error":"denied"}'})
        for body in ['<html>500</html>', '{', '{}', 'null', '[{}]',
                     '{"results":[],"count":1,"next":"/next"}',
                     '[{"id":"12345678-1234-4234-8234-123456789abc"}]']:
            with self.subTest(body=body):
                self.run_case({'body': body})
        self.run_case({'network': True})

    def test_exact_id_not_substring(self):
        self.run_case({'body': '[{"id":"different","filename":"12345678-1234-4234-8234-123456789abc"}]'}, success=True)

    def test_download_denied_missing_corrupted(self):
        for code in [401, 403, 404, 500, 302]:
            with self.subTest(code=code):
                self.run_case({'step': 4, 'code': code})
        self.run_case({'step': 4, 'body': 'corrupt'})
        self.run_case({'step': 4, 'body': ''})
        self.run_case({'step': 4, 'network': True})

    def test_other_stages_fail_closed(self):
        for step in [1, 2, 3, 5]:
            with self.subTest(step=step):
                self.run_case({'step': step, 'code': 403})
        self.run_case({'step': 1, 'body': '{"id":"../../unsafe"}'})
        self.run_case({'step': 2, 'body': '[]'})
        self.run_case({'step': 3, 'body': '{"id":"wrong","storage_key":"key"}'})


if __name__ == '__main__':
    unittest.main()
