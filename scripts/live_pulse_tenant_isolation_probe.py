#!/usr/bin/env python3
"""Read/write-safe live two-org isolation probe for Shizuha Pulse (PLAT-4196).

The probe uses only Shizuha-owned INTERNAL test principals and projects.  It
creates uniquely tagged disposable rows, verifies actor A cannot read or mutate
actor B's item/children even with B's org supplied in headers/query parameters,
checks own-tenant positive controls, and removes every created row.  Output is
limited to status codes, counts, hashes, release metadata, and cleanup state;
tokens and response bodies are never printed.

Required environment variables (or matching CLI flags):
  PULSE_PROBE_BASE_URL       e.g. https://shizuha.com/pulse/api
  PULSE_PROBE_TOKEN_A/B      internal test-principal bearer tokens
  PULSE_PROBE_PROJECT_A/B    numeric internal test project IDs in disjoint orgs
  PULSE_PROBE_ORG_A/B        numeric, disjoint organization IDs
  PULSE_PROBE_CUSTOM_FIELD_ID  disposable text field usable by project B

This script intentionally refuses identical org/project/token inputs and
refuses non-HTTPS remote targets (localhost is allowed for harness tests).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests


class ProbeFailure(RuntimeError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        results = payload.get('results', [])
        return [row for row in results if isinstance(row, dict)] if isinstance(results, list) else []
    return []


@dataclass(frozen=True)
class Actor:
    label: str
    token: str
    org_id: str
    project_id: str


class PulseProbe:
    def __init__(self, base_url: str, actor_a: Actor, actor_b: Actor,
                 custom_field_id: str, timeout: float, expected_release: str | None):
        self.base_url = base_url.rstrip('/')
        self.a = actor_a
        self.b = actor_b
        self.custom_field_id = custom_field_id
        self.timeout = timeout
        self.expected_release = expected_release
        self.created: list[tuple[Actor, str, str]] = []
        self.evidence: list[dict[str, Any]] = []

    def request(self, actor: Actor, method: str, path: str, *, expected: set[int],
                data: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None,
                extra_headers: dict[str, str] | None = None) -> tuple[requests.Response, Any]:
        headers = {
            'Authorization': f'Bearer {actor.token}',
            'Accept': 'application/json',
            'X-Organization-ID': actor.org_id,
        }
        if data is not None:
            headers['Content-Type'] = 'application/json'
        headers.update(extra_headers or {})
        response = requests.request(
            method, f'{self.base_url}/{path.lstrip("/")}', headers=headers,
            params=params, data=json.dumps(data) if data is not None else None,
            timeout=self.timeout, allow_redirects=False,
        )
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        self.evidence.append({
            'step': f'{actor.label}:{method}:{path.split("?")[0]}',
            'status': response.status_code,
        })
        if response.status_code not in expected:
            raise ProbeFailure(
                f'{actor.label} {method} {path}: status {response.status_code}, '
                f'expected {sorted(expected)} (body suppressed)')
        return response, payload

    def create(self, actor: Actor, path: str, data: dict[str, Any], kind: str) -> dict[str, Any]:
        _, payload = self.request(actor, 'POST', path, expected={201}, data=data)
        if not isinstance(payload, dict) or payload.get('id') is None:
            raise ProbeFailure(f'{kind} create returned no id (body suppressed)')
        self.created.append((actor, kind, str(payload['id'])))
        return payload

    def run(self) -> dict[str, Any]:
        nonce = secrets.token_hex(8)
        marker_a = f'PLAT4196-A-{nonce}'
        marker_b = f'PLAT4196-B-{nonce}'

        health, _ = self.request(self.a, 'GET', 'health/data/', expected={200})
        release = (
            health.headers.get('X-Shizuha-Release')
            or health.headers.get('X-Deployment-Sha')
            or health.headers.get('X-Image-Tag')
            or ''
        )
        if self.expected_release and self.expected_release not in release:
            raise ProbeFailure(
                f'expected release {self.expected_release!r}, observed header hash {_sha(release)!r}')

        item_a = self.create(self.a, 'items/', {
            'title': marker_a, 'description': 'internal isolation probe',
            'mode': 'task', 'project': self.a.project_id,
        }, 'item')
        item_b = self.create(self.b, 'items/', {
            'title': marker_b, 'description': 'internal isolation probe',
            'mode': 'task', 'project': self.b.project_id,
        }, 'item')
        id_a, id_b = str(item_a['id']), str(item_b['id'])

        comment_b = self.create(self.b, 'comments/', {
            'item': id_b, 'content': f'{marker_b}-comment',
        }, 'comment')
        worklog_b = self.create(self.b, 'work-logs/', {
            'item': id_b, 'duration_minutes': 1,
            'worked_date': datetime.now(timezone.utc).date().isoformat(),
            'description': f'{marker_b}-worklog',
        }, 'worklog')
        value_b = self.create(self.b, 'custom-field-values/', {
            'item': id_b, 'custom_field': self.custom_field_id,
            'value': f'{marker_b}-field',
        }, 'custom-field-value')

        # Positive controls prove both credentials and fixtures are live.
        _, own_a = self.request(self.a, 'GET', f'items/{id_a}/', expected={200})
        _, own_b = self.request(self.b, 'GET', f'items/{id_b}/', expected={200})
        if not isinstance(own_a, dict) or own_a.get('title') != marker_a:
            raise ProbeFailure('actor A positive-control marker mismatch (body suppressed)')
        if not isinstance(own_b, dict) or own_b.get('title') != marker_b:
            raise ProbeFailure('actor B positive-control marker mismatch (body suppressed)')

        smuggle_headers = {
            'X-Organization-ID': self.b.org_id,
            'X-Org-ID': self.b.org_id,
            'X-Shizuha-Organization-ID': self.b.org_id,
        }
        smuggle_query = {'organization_id': self.b.org_id, 'org_id': self.b.org_id}

        # Warm/read twice so an unsafe shared-cache key cannot hide behind a cold miss.
        for _ in range(2):
            self.request(self.a, 'GET', f'items/{id_b}/', expected={403, 404},
                         params=smuggle_query, extra_headers=smuggle_headers)
        for path in (
            f'comments/{comment_b["id"]}/',
            f'comments/{comment_b["id"]}/reactions/',
            f'work-logs/{worklog_b["id"]}/',
            'work-logs/by_item/',
            f'custom-field-values/{value_b["id"]}/',
        ):
            params = dict(smuggle_query)
            if path == 'work-logs/by_item/':
                params['item'] = id_b
            self.request(self.a, 'GET', path, expected={403, 404},
                         params=params, extra_headers=smuggle_headers)

        _, listed = self.request(
            self.a, 'GET', 'custom-field-values/', expected={200},
            params={**smuggle_query, 'item_id': id_b}, extra_headers=smuggle_headers)
        leaked_ids = {str(row.get('id')) for row in _rows(listed)}
        if str(value_b['id']) in leaked_ids:
            raise ProbeFailure('cross-org custom-field marker became list-visible')

        before = (own_b.get('status'), own_b.get('title'))
        _, transition = self.request(self.a, 'POST', 'items/bulk/', expected={200}, data={
            'item_ids': [id_b], 'operation': 'transition', 'data': {'to_status': 'done'},
        }, params=smuggle_query, extra_headers=smuggle_headers)
        if not isinstance(transition, dict) or transition.get('updated') != 0:
            raise ProbeFailure('cross-org bulk transition affected a row')
        _, deletion = self.request(self.a, 'POST', 'items/bulk/', expected={200}, data={
            'item_ids': [id_b], 'operation': 'delete',
        }, params=smuggle_query, extra_headers=smuggle_headers)
        if not isinstance(deletion, dict) or deletion.get('deleted') != 0:
            raise ProbeFailure('cross-org bulk delete affected a row')
        _, after_b = self.request(self.b, 'GET', f'items/{id_b}/', expected={200})
        if not isinstance(after_b, dict) or (after_b.get('status'), after_b.get('title')) != before:
            raise ProbeFailure('actor B fixture changed after actor A zero-effect probes')

        # Positive bulk authority + cleanup for A.  B cleanup runs in finally.
        _, own_delete = self.request(self.a, 'POST', 'items/bulk/', expected={200}, data={
            'item_ids': [id_a], 'operation': 'delete',
        })
        if not isinstance(own_delete, dict) or own_delete.get('deleted') != 1:
            raise ProbeFailure('actor A own-item bulk-delete positive control failed')
        self.created = [row for row in self.created if not (row[1] == 'item' and row[2] == id_a)]

        return {
            'result': 'PASS',
            'release_header_sha256_16': _sha(release),
            'marker_a_sha256_16': _sha(marker_a),
            'marker_b_sha256_16': _sha(marker_b),
            'checks': len(self.evidence),
        }

    def cleanup(self) -> list[str]:
        errors: list[str] = []
        # Item deletion cascades comments/work logs/custom values.  Delete items
        # last in creation order, then verify absence without printing bodies.
        item_rows = [row for row in self.created if row[1] == 'item']
        for actor, _, item_id in reversed(item_rows):
            try:
                self.request(actor, 'DELETE', f'items/{item_id}/', expected={204})
                self.request(actor, 'GET', f'items/{item_id}/', expected={404})
            except Exception as exc:  # preserve every cleanup attempt
                errors.append(f'{actor.label}:item:{item_id}:{type(exc).__name__}')
        self.created.clear()
        return errors


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=_env('PULSE_PROBE_BASE_URL'))
    parser.add_argument('--token-a', default=_env('PULSE_PROBE_TOKEN_A'))
    parser.add_argument('--token-b', default=_env('PULSE_PROBE_TOKEN_B'))
    parser.add_argument('--project-a', default=_env('PULSE_PROBE_PROJECT_A'))
    parser.add_argument('--project-b', default=_env('PULSE_PROBE_PROJECT_B'))
    parser.add_argument('--org-a', default=_env('PULSE_PROBE_ORG_A'))
    parser.add_argument('--org-b', default=_env('PULSE_PROBE_ORG_B'))
    parser.add_argument('--custom-field-id', default=_env('PULSE_PROBE_CUSTOM_FIELD_ID'))
    parser.add_argument('--expected-release', default=_env('PULSE_PROBE_EXPECTED_RELEASE'))
    parser.add_argument('--timeout', type=float, default=20.0)
    args = parser.parse_args(argv)
    required = ('base_url', 'token_a', 'token_b', 'project_a', 'project_b',
                'org_a', 'org_b', 'custom_field_id')
    missing = [name.replace('_', '-') for name in required if not getattr(args, name)]
    if missing:
        parser.error(f'missing required inputs: {", ".join(missing)}')
    parsed = urllib.parse.urlparse(args.base_url)
    if parsed.scheme != 'https' and parsed.hostname not in {'127.0.0.1', 'localhost'}:
        parser.error('base URL must be HTTPS (except localhost harnesses)')
    if args.org_a == args.org_b:
        parser.error('org A and org B must be disjoint')
    if args.project_a == args.project_b:
        parser.error('project A and project B must be disjoint')
    if secrets.compare_digest(args.token_a, args.token_b):
        parser.error('actor A and actor B tokens must be distinct')
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    probe = PulseProbe(
        args.base_url,
        Actor('A', args.token_a, str(args.org_a), str(args.project_a)),
        Actor('B', args.token_b, str(args.org_b), str(args.project_b)),
        str(args.custom_field_id), args.timeout, args.expected_release,
    )
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = probe.run()
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    cleanup_errors = probe.cleanup()
    if error or cleanup_errors:
        print(json.dumps({
            'result': 'FAIL', 'error': error,
            'cleanup_errors': cleanup_errors,
            'checks': len(probe.evidence),
        }, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({**(result or {}), 'cleanup': 'PASS'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
