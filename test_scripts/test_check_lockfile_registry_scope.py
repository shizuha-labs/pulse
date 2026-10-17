"""Controls for the lockfile scope->registry guard (PLAT-5031).

Every case below is a REAL defect or a real compliant state observed on
2026-07-22, not an invented example. The two failing directions each cost the
fleet real downtime:

  PRIVATE_NOT_INTERNAL  HIVE-1270 -- three days of blocked hive-frontend deploys
  PUBLIC_NOT_NPMJS      hive#50 first commit -- my own defect; a host-only
                        checker passed it, which is why both directions are
                        asserted here rather than one.

The negative controls matter more than the positives: a guard that cannot fail
is indistinguishable from one that is not running.
"""
import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / 'scripts' / 'check_lockfile_registry_scope.py'
INTERNAL = 'http://npm-cache.registry.svc.cluster.local:4873'


def _lock(tmp_path, packages):
    (tmp_path / 'package-lock.json').write_text(json.dumps({'packages': packages}))
    return subprocess.run([sys.executable, str(GUARD), str(tmp_path)],
                          capture_output=True, text=True)


def test_private_scope_on_public_registry_fails(tmp_path):
    """mode 2 -- @shizuha/* 404s on npmjs at any version (drive, id, connect)."""
    r = _lock(tmp_path, {'node_modules/@shizuha/ui': {
        'resolved': 'https://registry.npmjs.org/@shizuha/ui/-/ui-1.0.0.tgz'}})
    assert r.returncode == 1
    assert 'PRIVATE_NOT_INTERNAL' in r.stdout


def test_public_package_on_internal_registry_fails(tmp_path):
    """mode 1 -- unreachable off-cluster. This is the defect I shipped in
    hive#50's first commit and my host-only checker certified as clean."""
    r = _lock(tmp_path, {'node_modules/react': {
        'resolved': INTERNAL + '/react/-/react-18.2.0.tgz'}})
    assert r.returncode == 1
    assert 'PUBLIC_NOT_NPMJS' in r.stdout


def test_node_ip_pin_fails(tmp_path):
    """The original PLAT-5031 shape: a NodePort on a node IP."""
    r = _lock(tmp_path, {'node_modules/react': {
        'resolved': 'http://100.64.0.3:30512/react/-/react-18.2.0.tgz'}})
    assert r.returncode == 1


def test_unparseable_resolved_fails_closed(tmp_path):
    """An unexamined entry is not a clean entry -- must not be skipped."""
    r = _lock(tmp_path, {'node_modules/x': {'resolved': 'garbage-not-a-url'}})
    assert r.returncode == 1
    assert 'UNPARSEABLE' in r.stdout


def test_correct_contract_passes(tmp_path):
    """POSITIVE control: both directions correct. If this ever fails, the guard
    rejects the state the CoS ruling mandates."""
    r = _lock(tmp_path, {
        'node_modules/react': {'resolved': 'https://registry.npmjs.org/react/-/react-18.2.0.tgz'},
        'node_modules/@shizuha/ui': {'resolved': INTERNAL + '/@shizuha/ui/-/ui-1.2.1.tgz'}})
    assert r.returncode == 0


def test_workspace_reference_passes(tmp_path):
    """POSITIVE control: local refs carry no registry and are legitimate."""
    r = _lock(tmp_path, {'packages/a': {'resolved': './packages/a'}})
    assert r.returncode == 0


def test_no_lockfile_found_exits_2(tmp_path):
    """Scanning nothing must not report clean -- 'unmeasured' is not 'passing'."""
    r = subprocess.run([sys.executable, str(GUARD), str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_public_package_nested_under_private_scope_passes(tmp_path):
    """REGRESSION (reika, PLAT-5031 review of #98 @ ab5369c).

    A public package nested under a private one has the private scope in its
    PATH but not in its own name. Substring-matching the path classified it as
    private and demanded it sit on the internal registry -- a false positive on
    a correct lockfile. `drive` carries a live instance.

    This is the control I omitted: I tested private and public packages, but
    never a public package whose path contained a private scope.
    """
    r = _lock(tmp_path, {
        'node_modules/@shizuha/ui': {'resolved': INTERNAL + '/@shizuha/ui/-/ui-1.2.1.tgz'},
        'node_modules/@shizuha/ui/node_modules/lucide-react': {
            'resolved': 'https://registry.npmjs.org/lucide-react/-/lucide-react-0.303.0.tgz'},
    })
    assert r.returncode == 0, r.stdout


def test_private_package_nested_deeper_still_requires_internal(tmp_path):
    """Converse control: the narrower test must not now UNDER-match.

    A private package nested under another package is still private and must
    still be rejected on the public registry.
    """
    r = _lock(tmp_path, {'node_modules/some-pkg/node_modules/@shizuha/ui': {
        'resolved': 'https://registry.npmjs.org/@shizuha/ui/-/ui-1.0.0.tgz'}})
    assert r.returncode == 1
    assert 'PRIVATE_NOT_INTERNAL' in r.stdout
