#!/usr/bin/env python3
"""Validate package-lock.json `resolved` hosts against the scope->registry contract.

CoS ruling 2026-07-22 (PLAT-5031; Verdaccio runbook v22 e098c698), BOTH directions:

    public package      -> registry.npmjs.org                          (public only)
    @shizuha/* package  -> npm-cache.registry.svc.cluster.local:4873   (internal only)

Rationale for each direction, from real incidents:

  public-on-internal  (mode 1) A pin to the in-cluster cache -- or worse a node-IP
                      NodePort -- is unreachable off-cluster, so `npm ci` cannot run
                      on a runner without cluster access. `--registry` cannot override
                      it: npm honours `resolved` verbatim. PLAT-5031 found 1018 such
                      pins across 6 repos. I then reintroduced one myself in hive#50's
                      first commit by repointing public packages to the cache, and my
                      own host-only checker passed it -- which is why this direction
                      is enforced rather than assumed.

  private-on-public   (mode 2) `@shizuha/*` is a private scope; it 404s on the public
                      registry at ANY version. A lockfile can therefore be entirely
                      host-clean and still uninstallable. HIVE-1270 cost hive-frontend
                      three days of blocked deploys this way; drive/id/connect carried
                      the same defect latently.

Exit codes: 0 clean, 1 violations found, 2 nothing scanned (fail closed).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PUBLIC_REGISTRY = 'registry.npmjs.org'
# PLS-1409 / CON-466: the internal npm registry host is first-party topology
# and is env-configurable for third-party deployments.
INTERNAL_REGISTRY = os.environ.get(
    'PULSE_INTERNAL_NPM_REGISTRY', 'npm-cache.registry.svc.cluster.local:4873'
)
PRIVATE_SCOPE = '@shizuha/'

# Local/workspace references carry no registry and are always legitimate.
WORKSPACE_PREFIXES = ('./', '../', 'file:', 'link:', 'workspace:')

_HOST_RE = re.compile(r'^https?://([^/]+)/')


def classify(name: str, resolved: str) -> tuple[str, str] | None:
    """Return (code, detail) for a violation, or None when the entry is compliant."""
    if resolved.startswith(WORKSPACE_PREFIXES):
        return None

    match = _HOST_RE.match(resolved)
    if not match:
        # Fail CLOSED. An unparseable `resolved` is unexamined, not clean --
        # skipping it is how a checker reports a confident pass over a case it
        # never looked at.
        return 'UNPARSEABLE', resolved

    host = match.group(1)
    # Classify by the package's OWN name, not the whole lockfile path. A public
    # package nested under a private one has a path containing the private scope
    # -- e.g. node_modules/@shizuha/ui/node_modules/lucide-react -- and a
    # substring test misreads it as private, then demands it live on the
    # internal registry. `drive` carries a live instance of exactly that.
    own_name = name.rsplit('node_modules/', 1)[-1]
    is_private = own_name.startswith(PRIVATE_SCOPE)

    if is_private and host != INTERNAL_REGISTRY:
        return 'PRIVATE_NOT_INTERNAL', host
    if not is_private and host != PUBLIC_REGISTRY:
        return 'PUBLIC_NOT_NPMJS', host
    return None


def scan(path: Path) -> list[tuple[str, str, str]]:
    data = json.loads(path.read_text())
    violations = []
    for name, pkg in (data.get('packages') or {}).items():
        resolved = pkg.get('resolved')
        if not resolved:
            continue
        verdict = classify(name, resolved)
        if verdict:
            violations.append((name, *verdict))
    return violations


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path('.')]
    lockfiles = []
    for root in roots:
        if root.is_file():
            lockfiles.append(root)
            continue
        lockfiles.extend(
            p for p in root.rglob('package-lock.json') if 'node_modules' not in p.parts
        )

    if not lockfiles:
        print('ERROR: no package-lock.json found — refusing to report clean', file=sys.stderr)
        return 2

    total = 0
    for lock in sorted(lockfiles):
        violations = scan(lock)
        total += len(violations)
        if violations:
            print(f'\n{lock}: {len(violations)} violation(s)')
            for name, code, detail in violations[:20]:
                print(f'  {code:20s} {name}  ->  {detail}')
            if len(violations) > 20:
                print(f'  ... and {len(violations) - 20} more')
        else:
            print(f'{lock}: OK')

    if total:
        print(f'\nFAILED: {total} violation(s). Contract: public -> {PUBLIC_REGISTRY}; '
              f'{PRIVATE_SCOPE}* -> {INTERNAL_REGISTRY}')
        return 1
    print(f'\nOK: {len(lockfiles)} lockfile(s) satisfy the scope->registry contract')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
