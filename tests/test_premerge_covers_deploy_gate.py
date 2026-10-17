"""PLAT-8484: the premerge suite must cover the deploy gate's suite.

Suite-scope asymmetry between the gates let merge 06f4dbf sail through
premerge green and detonate on the deploy lane (22 failures + 184 errors,
30m production deploy block). The deploy gate runs legs premerge did not:
the static authz-scope invariant + live tenant-isolation probe
(``test_scripts.*`` — top-level modules outside the ``tasks`` Django
discovery scope) and ``scripts/check_drf_authz_scope.py``.

This guard fails the gate when the deploy workflow names a test module the
premerge workflow does not cover — explicitly, or via a package whose
Django discovery picks the module up (e.g. ``tasks`` discovers everything
under ``tasks/tests/``). Future suite edits cannot silently reopen the gap.
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DEPLOY = ROOT / ".forgejo" / "workflows" / "deploy.yml"
PREMERGE = ROOT / ".forgejo" / "workflows" / "premerge.yml"


def _leg_blocks(text):
    """Yield continuation-joined blocks for unittest/manage.py-test legs."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"manage\.py\s+test\b|\bm?\s*unittest\b.*-q\b|python\s+-m\s+unittest\b", line):
            block = [line]
            # Walk continuation lines: trailing backslash keeps the leg going.
            while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                block.append(lines[i])
            blocks.append(" ".join(block))
        i += 1
    return blocks


def _explicit_modules(text):
    """Test-module tokens named explicitly in test legs of a workflow."""
    markers = (
        "manage.py", "unittest", "pytest", "-m", "python", "test",
        "-q", "||", "{", "}", "exit", "echo", "export", "resources",
        "limits", "requests", "volumeMounts", "volumes", "YAML", "||",
    )
    modules = set()
    for block in _leg_blocks(text):
        for token in re.split(r"[\s\\]+", block):
            token = token.strip().strip("\\")
            if not token or token.startswith("--") or token.startswith("$"):
                continue
            if token in markers or "=" in token:
                continue
            # Module-like: dotted path or a Django package name.
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+", token) \
                    or token in {"tasks", "alertdelivery"}:
                modules.add(token)
    return modules


def _discovered_modules(package):
    """Test modules Django discovery picks up under ``package/tests/``."""
    base = ROOT / package / "tests"
    found = set()
    if not base.is_dir():
        return found
    for path in base.rglob("test_*.py"):
        rel = path.relative_to(ROOT).with_suffix("")
        found.add(".".join(rel.parts))
    return found


def _premerge_coverage():
    """Modules premerge covers: explicit + discovered via its packages."""
    text = PREMERGE.read_text()
    covered = _explicit_modules(text)
    for package in {"tasks", "alertdelivery"}:
        if package in covered:
            covered |= _discovered_modules(package)
    return covered


class PremergeCoversDeployGateTests(unittest.TestCase):
    def test_premerge_covers_every_deploy_gate_module(self):
        """Every test module the deploy gate names must be covered by premerge."""
        deploy_modules = _explicit_modules(DEPLOY.read_text())
        self.assertTrue(deploy_modules, "deploy workflow must name test modules")
        covered = _premerge_coverage()
        uncovered = sorted(deploy_modules - covered)
        self.assertEqual(
            uncovered, [],
            "PLAT-8484: premerge does not cover deploy-gate-only test modules "
            "%s — a merge breaking them lands green-on-premerge, RED-on-deploy. "
            "Add the missing legs to premerge.yml." % uncovered,
        )

    def test_deploy_gate_names_the_incident_boundary(self):
        """The static authz-scope invariant + live tenant probe stay wired."""
        text = DEPLOY.read_text()
        self.assertIn("test_scripts.test_check_drf_authz_scope", text)
        self.assertIn("test_scripts.test_live_pulse_tenant_isolation_probe", text)
        self.assertIn("scripts/check_drf_authz_scope.py", text)


if __name__ == "__main__":
    unittest.main()
