"""PLS-1249: CI-only / no-deploy-surface evidence class for ORIGIN tasks.

A dev task whose merged fix is test-only/docs-only/CI-config-only has no
deploy surface: a correlated production DeploymentEvent is structurally
unsatisfiable for its close (the deploy-verify gate's ``live`` path demands
one that can never exist). The gate derives this class from the MERGED
CONTENT — every changed file across all merged linked PRs must be a
test/doc/CI-config path — and then accepts the merged head's green CI run
as the verification evidence instead.

Fail-closed by construction: any Forgejo fetch error, an unknown path, or a
non-green head keeps the governed deploy-verify path (the class never
loosens a gate that would otherwise pass).
"""
from __future__ import annotations

import fnmatch
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# PLAT-11436 review-3: the gate evaluation establishes ONE wall-clock budget
# (``budget_end`` — a ``time.monotonic()`` absolute) and every wire leg under
# it — the compare leg, these files/status fetches — shares it. Admission is
# a module-level BOUNDED SEMAPHORE acquired non-blocking (no counter races
# across threads); the slot is released by the wire task itself in its
# ``finally`` (a timed-out read holds its slot until the read finishes); when
# the pool is saturated the call refuses WITHOUT wire — slow Origin can never
# hold more than 4 slots nor spawn more than 4 threads. _COMPLETED (lock-
# guarded) is completion bookkeeping for hermetic test drains only.
_MAX_IN_FLIGHT = 4
_SLOT_SEM = threading.BoundedSemaphore(_MAX_IN_FLIGHT)
_ACTIVE_LOCK = threading.Lock()
_ADMITTED = 0
_COMPLETED = 0
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_DEFAULT_FALLBACK_SECONDS = 20.0

# Conservative CI-only path classes. Deliberately narrow: a path that is not
# unambiguously test/doc/CI-config keeps the governed path.
_CI_ONLY_DIR_PREFIXES = (
    'tests/', 'test/', 'docs/', 'doc/', '.forgejo/', '.github/',
    'scripts/ci/', 'ci/',
)
_CI_ONLY_BASENAME_PATTERNS = (
    'test_*.py', '*_test.py', '*_test.go', '*.test.ts', '*.test.tsx',
    '*.test.js', '*.test.jsx', '*.spec.ts', '*.spec.tsx', '*.spec.js',
    '*.spec.jsx', 'conftest.py', 'testing.md', 'makefile',
)
_CI_ONLY_SUFFIXES = (
    '.md', '.rst', '.txt',  # docs
    '.cfg', '.ini',  # lint/tool config
)
# CI workflow / lint config files at repo root or anywhere.
_CI_ONLY_EXACT = {
    '.forgejo/workflows', '.github/workflows', 'pyproject.toml',
    'setup.cfg', 'tox.ini', '.eslintrc', '.eslintrc.js', '.eslintrc.json',
    'tsconfig.json', 'vitest.config.ts', 'jest.config.js', 'pytest.ini',
    '.flake8', '.isort.cfg', '.pre-commit-config.yaml',
}
# Suffixes that are config ONLY in CI/workflow context — a bare .yml/.yaml
# elsewhere could be a deploy manifest, so those are matched by directory
# prefix (.forgejo/, .github/) instead of suffix.
_CI_ONLY_YAML_DIRS = ('.forgejo/', '.github/', 'ci/', 'scripts/ci/')

_FILES_CAP = 100  # per PR; a CI-only fix never approaches this


def _forgejo_api_url() -> str:
    import os
    base = os.environ.get(
        'ORIGIN_API_URL', os.environ.get('ORIGIN_PUBLIC_URL', 'https://forgejo.example.invalid').rstrip('/') + '/api/v1',
    ).rstrip('/')
    # PLS-1249 follow-up: the shizuha-admin chart sets ORIGIN_API_URL to the
    # BARE Forgejo host (http://forgejo-http.origin.svc.cluster.local) while
    # this module builds paths that assume the full API base …/api/v1. A bare
    # host produced /repos/... 404s → every fetch failed closed and the
    # CI-only class could never fire. Normalize instead of trusting the
    # config shape.
    if not base.endswith('/api/v1'):
        base = f'{base}/api/v1'
    return base


def _forgejo_token() -> str:
    import os
    return os.environ.get('FORGEJO_TOKEN', '').strip()


def _http_json(url: str) -> Optional[object]:
    import requests
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'token {_forgejo_token()}'},
            # PLS-1462: the deploy-verify gate calls this synchronously, once
            # per merged PR (files) and once per head (status). 15s each held
            # the worker past the 30s/60s client deadlines (~121s on
            # HIVE-2707) before the gate could refuse. A short timeout fails
            # closed (caller treats None/False as not-evidence) instead of
            # hanging the transition.
            timeout=4,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_IN_FLIGHT)
    return _EXECUTOR


def _run_and_release(fn):
    try:
        return fn()
    finally:
        global _COMPLETED
        with _ACTIVE_LOCK:
            _COMPLETED += 1
        _SLOT_SEM.release()


def _slots_in_flight():
    """Slots currently claimed = acquired minus released-and-completed."""
    import threading as _t
    # BoundedSemaphore exposes no portable counter; derive from completions
    # vs admissions via a lock-guarded counter pair.
    with _ACTIVE_LOCK:
        return _ADMITTED - _COMPLETED


def _budget_call(fn, budget_end):
    """Run ``fn`` under the shared wall-clock budget. Returns the result, or
    None when the budget is spent, the pool is saturated, or the call tripped
    the deadline — every None is a fail-closed 'unknown' for the caller."""
    global _ADMITTED
    import time as _time
    now = _time.monotonic()
    if budget_end is not None and now >= budget_end:
        return None
    if not _SLOT_SEM.acquire(blocking=False):
        return None
    with _ACTIVE_LOCK:
        _ADMITTED += 1
    future = _executor().submit(_run_and_release, fn)
    timeout = (
        max(budget_end - now, 0.0)
        if budget_end is not None
        else _DEFAULT_FALLBACK_SECONDS
    )
    try:
        return future.result(timeout=timeout)
    except Exception:
        return None


def _path_is_ci_only(path: str) -> bool:
    p = (path or '').strip()
    if p.startswith('./'):
        p = p[2:]
    if not p:
        return False
    lowered = p.lower()
    if any(lowered.startswith(d) for d in _CI_ONLY_DIR_PREFIXES):
        return True
    if any(lowered.startswith(d) for d in _CI_ONLY_YAML_DIRS) and lowered.endswith(('.yml', '.yaml')):
        return True
    base = lowered.rsplit('/', 1)[-1]
    if base in _CI_ONLY_EXACT:
        return True
    if any(fnmatch.fnmatch(base, pat) for pat in _CI_ONLY_BASENAME_PATTERNS):
        return True
    if base.endswith(_CI_ONLY_SUFFIXES):
        return True
    return False


def pr_files_are_ci_only(
    repo_full_name: str, pr_number: int, budget_end=None,
) -> Optional[bool]:
    """True when EVERY changed file in the PR is CI-only; False when any is
    not; None when the fetch failed (unknown — callers fail closed)."""
    if not repo_full_name or not pr_number:
        return None
    url = f'{_forgejo_api_url()}/repos/{repo_full_name}/pulls/{int(pr_number)}/files'
    files = _budget_call(lambda: _http_json(url), budget_end)
    if not isinstance(files, list) or not files:
        return None  # empty/failed fetch is unknown, not CI-only
    if len(files) >= _FILES_CAP:
        return False  # implausible for a CI-only fix — fail closed
    for f in files:
        path = str((f or {}).get('filename') or '')
        if not _path_is_ci_only(path):
            return False
    return True


def head_ci_green(repo_full_name: str, sha: str, budget_end=None) -> bool:
    """True when the combined commit status for the merged head is success."""
    if not repo_full_name or not sha:
        return False
    url = f'{_forgejo_api_url()}/repos/{repo_full_name}/commits/{sha}/status'
    data = _budget_call(lambda: _http_json(url), budget_end)
    if not isinstance(data, dict):
        return False
    return str(data.get('state') or '').strip().lower() == 'success'
