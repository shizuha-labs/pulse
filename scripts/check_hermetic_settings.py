#!/usr/bin/env python3
"""PLAT-1358 hermetic Python test-settings CI check.

Static gate: every Django/Python service repo must have an explicit test settings
module and CI must invoke tests with that module. Dynamic network blocking is
provided by templates/conftest_hermetic.py.template + scripts/hermetic_socket_guard.py.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass

from hermetic_socket_guard import HermeticityError, socket_guard

PROD_SETTING_MARKERS = ('shizuha-id', 'shizuha-connect', 'postgres.shizuha', 'redis://:@localhost:7379')
TEST_SETTINGS_NAMES = ('test_settings.py', 'local_sqlite_test_settings.py', 'sqlite_test_settings.py')


@dataclass
class Finding:
    level: str
    message: str


def rel(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def discover_settings_modules(root: pathlib.Path) -> list[pathlib.Path]:
    ignored = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'migrations'}
    modules: list[pathlib.Path] = []
    for path in root.rglob('settings.py'):
        if any(part in ignored for part in path.parts):
            continue
        if path.parent == root:
            continue
        modules.append(path)
    return sorted(modules)


def module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    return '.'.join(path.relative_to(root).with_suffix('').parts)


def find_test_settings_for(settings_path: pathlib.Path) -> pathlib.Path | None:
    for name in TEST_SETTINGS_NAMES:
        candidate = settings_path.with_name(name)
        if candidate.exists():
            return candidate
    return None


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def validate_test_settings(path: pathlib.Path, root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    text = read(path)
    display = rel(path, root)
    if not re.search(r'\bTESTING\s*=\s*True\b', text):
        findings.append(Finding('error', f'{display}: must set TESTING = True'))
    if 'DATABASES' not in text:
        findings.append(Finding('error', f'{display}: must define hermetic DATABASES (SQLite or CI-owned local Postgres)'))
    has_socket_guard = 'HermeticDiscoverRunner' in text or 'socket_guard' in text or (root / 'conftest.py').exists()
    if not has_socket_guard:
        findings.append(Finding('error', f'{display}: must install a no-outbound socket guard (TEST_RUNNER or conftest.py)'))
    if not re.search(r'(from\s+\.settings\s+import\s+\*|import\s+.*settings)', text):
        findings.append(Finding('warning', f'{display}: should inherit from production settings then override unsafe dependencies'))
    lowered = text.lower()
    for marker in PROD_SETTING_MARKERS:
        if marker in lowered and '127.0.0.1:9' not in lowered:
            findings.append(Finding('error', f'{display}: appears to reference production/shared dependency marker {marker!r}'))
    return findings


def workflow_texts(root: pathlib.Path, workflow_paths: list[pathlib.Path] | None) -> list[tuple[pathlib.Path, str]]:
    if workflow_paths:
        paths = workflow_paths
    else:
        wf_dir = root / '.forgejo' / 'workflows'
        paths = sorted(list(wf_dir.glob('*.yml')) + list(wf_dir.glob('*.yaml'))) if wf_dir.exists() else []
    return [(p, read(p)) for p in paths if p.exists()]


def validate_ci_invocation(root: pathlib.Path, settings_modules: list[str], workflow_paths: list[pathlib.Path] | None) -> list[Finding]:
    findings: list[Finding] = []
    workflows = workflow_texts(root, workflow_paths)
    if not workflows:
        findings.append(Finding('warning', 'no Forgejo workflow files found; skipping CI invocation check'))
        return findings
    combined = '\n'.join(text for _, text in workflows)
    test_lines = [line.strip() for line in combined.splitlines() if 'manage.py test' in line and not line.strip().startswith('#')]
    if not test_lines:
        findings.append(Finding('warning', 'no manage.py test invocation found in workflows'))
    for module in settings_modules:
        explicit = f'--settings={module}' in combined or f'DJANGO_SETTINGS_MODULE={module}' in combined
        if not explicit:
            findings.append(Finding('error', f'CI does not invoke tests with explicit hermetic settings module {module!r}'))
    for line in test_lines:
        if '--settings=' not in line and 'DJANGO_SETTINGS_MODULE=' not in line:
            findings.append(Finding('error', f'CI test invocation lacks explicit --settings: {line}'))
    if not re.search(r'(timeout-minutes|--timeout=|DEADLINE=|timeout\s+\d+)', combined):
        findings.append(Finding('error', 'CI test job must have a wall-clock timeout/fail-loud deadline'))
    return findings


def run_check(root: pathlib.Path, explicit_settings: list[str], workflow_paths: list[pathlib.Path] | None) -> int:
    findings: list[Finding] = []
    settings_paths = discover_settings_modules(root)
    if not settings_paths:
        findings.append(Finding('error', 'no Django settings.py modules found'))
    test_modules: list[str] = []
    explicit_paths = [root / pathlib.Path(m.replace('.', '/') + '.py') for m in explicit_settings]
    if explicit_paths:
        for path in explicit_paths:
            if not path.exists():
                findings.append(Finding('error', f'explicit settings module missing: {rel(path, root)}'))
            else:
                test_modules.append(module_name(path, root))
                findings.extend(validate_test_settings(path, root))
    else:
        for settings_path in settings_paths:
            candidate = find_test_settings_for(settings_path)
            if candidate is None:
                findings.append(Finding('error', f'{rel(settings_path.parent, root)}: missing explicit test_settings.py (or local_sqlite_test_settings.py)'))
            else:
                test_modules.append(module_name(candidate, root))
                findings.extend(validate_test_settings(candidate, root))
    findings.extend(validate_ci_invocation(root, test_modules, workflow_paths))

    errors = [f for f in findings if f.level == 'error']
    warnings = [f for f in findings if f.level == 'warning']
    for f in errors + warnings:
        prefix = 'ERROR' if f.level == 'error' else 'WARN'
        print(f'{prefix}: {f.message}', file=sys.stderr)
    if errors:
        print(f'PLAT-1358 hermetic settings check failed: {len(errors)} error(s), {len(warnings)} warning(s).', file=sys.stderr)
        return 1
    print(f'PLAT-1358 hermetic settings check passed ({len(test_modules)} test settings module(s), {len(warnings)} warning(s)).')
    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / 'svc').mkdir()
        (root / 'svc' / '__init__.py').write_text('', encoding='utf-8')
        (root / 'svc' / 'settings.py').write_text('DATABASES = {}\n', encoding='utf-8')
        (root / '.forgejo' / 'workflows').mkdir(parents=True)
        (root / '.forgejo' / 'workflows' / 'deploy.yml').write_text('run: python manage.py test svc\n', encoding='utf-8')
        assert run_check(root, [], None) == 1
        (root / 'svc' / 'test_settings.py').write_text(textwrap.dedent('''
            from .settings import *
            TESTING = True
            TEST_RUNNER = 'svc.test_runner.HermeticDiscoverRunner'
            DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
        '''), encoding='utf-8')
        (root / '.forgejo' / 'workflows' / 'deploy.yml').write_text(
            'timeout-minutes: 10\nrun: python manage.py test svc --settings=svc.test_settings\n', encoding='utf-8')
        assert run_check(root, [], None) == 0
    try:
        with socket_guard(allow_loopback=True):
            socket.create_connection(('example.com', 80), timeout=0.01)
    except HermeticityError:
        pass
    else:
        print('ERROR: socket guard did not block outbound connection', file=sys.stderr)
        return 1
    print('Self-test passed: static checker catches missing settings and socket guard blocks outbound connects.')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='repository/service root to scan')
    parser.add_argument('--settings-module', action='append', default=[], help='explicit hermetic settings module (repeatable)')
    parser.add_argument('--workflow', action='append', default=[], help='workflow file to inspect (default: .forgejo/workflows/*.yml)')
    parser.add_argument('--self-test', action='store_true', help='run checker/socket-guard self-test')
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    root = pathlib.Path(args.root).resolve()
    workflows = [pathlib.Path(p).resolve() for p in args.workflow]
    return run_check(root, args.settings_module, workflows or None)


if __name__ == '__main__':
    raise SystemExit(main())
