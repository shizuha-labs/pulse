#!/usr/bin/env python3
"""PLS-717 test-discovery guard.

Every test file in the repository must live in a path a required CI job
discovers, or be explicitly listed here as non-CI with a reason. A test file
placed anywhere else is committed, reviewed, and never executed — the
"mechanism present, outcome unreachable" failure at the test-discovery layer
(PLS-717). This guard fails the premerge gate instead of letting such a file
silently gate nothing.

Discovered paths (required CI jobs):
- ``tasks/tests/``            -> ``manage.py test tasks alertdelivery`` (premerge)
- ``tasks/test_*.py``         -> ``manage.py test tasks alertdelivery`` discovers the
                                 tasks package (premerge) + security-ci-parity.yml
- ``alertdelivery/tests/``    -> ``manage.py test tasks alertdelivery`` (premerge,
                                 PLS-717: second Django app, previously undiscovered)
- root ``tests/``             -> ``python -m pytest tests/`` (premerge, PLS-717)
- ``mcp-server/test_*.py``    -> mcp-contract-tests.yml (named unittest list)
- ``test_scripts/test_*.py``  -> deploy.yml (named unittest list)
- ``.forgejo/scripts/test_*.py`` -> deploy-frontend.yml (runs test_exact_deployment_pods.py)

Exit codes: 0 = all test files are discovered or explicitly non-CI;
1 = one or more test files are neither (listed on stderr).
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Paths a required CI job discovers (relative to repo root).
DISCOVERED_PREFIXES = (
    "tasks/tests/",
    "tasks/test_",
    "tests/",
    "alertdelivery/tests/",
    "mcp-server/test_",
    "test_scripts/",
    ".forgejo/scripts/test_",
)

# Non-test infrastructure files whose *name* looks like a test but are settings
# /runner modules, not test cases. They are not collected by any test runner.
NON_TEST_FILES = {
    "taskmanager/settings_test.py",
    "taskmanager/test_runner.py",
    "taskmanager/test_settings.py",
}

# Explicitly non-CI test files with a recorded reason (acceptance #1: "or
# explicitly listed as non-CI with a reason"). These are pytest-based
# Django-integration tests that require the full test-DB/Redis setup and are
# deliberately excluded from premerge (which scopes to `manage.py test tasks`
# to avoid collecting them). They are tracked separately from the static
# workflow tests in tests/.
NON_CI_TEST_FILES = {
    "test_enhanced_api.py": "pytest-based Django-integration test; requires full test-DB setup; excluded from premerge discovery (PLS-229).",
    "test_log_redaction.py": "pytest-based Django-integration test; requires full test-DB setup; excluded from premerge discovery (PLS-229).",
    "test_plat419_review_selfloop.py": "pytest-based Django-integration test; requires full test-DB setup; excluded from premerge discovery (PLS-229).",
    "test_plat855_security_review_fallback.py": "pytest-based Django-integration test; requires full test-DB setup; excluded from premerge discovery (PLS-229).",
}


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def main() -> int:
    violations = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if not _is_test_file(path):
            continue
        # Skip vendored/third-party trees that are not ours to police.
        if rel.parts and rel.parts[0] in (".venv", "node_modules", ".git"):
            continue
        rel_str = str(rel)
        if rel_str in NON_TEST_FILES:
            continue
        if rel_str in NON_CI_TEST_FILES:
            continue
        if any(rel_str.startswith(prefix) for prefix in DISCOVERED_PREFIXES):
            continue
        violations.append(rel_str)

    if violations:
        print(
            "PLS-717 test-discovery guard FAILED: test file(s) outside a "
            "discovered path would never run in CI:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "Move the file into a discovered path (tasks/tests/, tasks/test_*, "
            "alertdelivery/tests/, root tests/, mcp-server/test_*, test_scripts/, "
            ".forgejo/scripts/test_*) "
            "or record it as explicitly non-CI in scripts/check_test_discovery.py.",
            file=sys.stderr,
        )
        return 1

    print("PLS-717 test-discovery guard OK: all test files are discovered or non-CI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
