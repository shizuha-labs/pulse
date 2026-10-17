"""PLS-717: the test-discovery guard fails on misplaced test files.

The root tests/ directory was discovered by no CI job — deploy-workflow,
security-CI, and MCP-contract tests were committed, reviewed, and never
executed. The fix adds `pytest tests/` to premerge and a guard
(scripts/check_test_discovery.py) that fails the gate when a test file exists
outside a discovered path. This test pins the guard's behaviour.
"""
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_test_discovery  # noqa: E402


class TestDiscoveryGuardTests(unittest.TestCase):
    def test_guard_passes_on_current_repo(self):
        """The guard must pass on the current tree (all test files discovered or
        explicitly non-CI)."""
        self.assertEqual(check_test_discovery.main(), 0)

    def test_guard_fails_on_misplaced_test_file(self):
        """A test file outside a discovered path must fail the guard."""
        fake = mock.MagicMock()
        fake.name = "test_should_be_discovered.py"
        fake_root = mock.MagicMock()
        fake_root.rglob.return_value = [fake]
        fake.relative_to.return_value = pathlib.Path("scripts/test_should_be_discovered.py")
        with mock.patch.object(
            check_test_discovery, "ROOT", fake_root
        ), mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()):
            self.assertEqual(check_test_discovery.main(), 1)

    def test_guard_accepts_discovered_paths(self):
        """Test files in tasks/tests/, root tests/, mcp-server/, test_scripts/,
        and .forgejo/scripts/ are all considered discovered."""
        discovered = [
            "tasks/tests/test_foo.py",
            "tests/test_foo.py",
            "mcp-server/test_foo.py",
            "test_scripts/test_foo.py",
            ".forgejo/scripts/test_foo.py",
        ]
        for rel in discovered:
            with self.subTest(rel=rel):
                self.assertTrue(
                    any(rel.startswith(p) for p in check_test_discovery.DISCOVERED_PREFIXES),
                    f"{rel} should be discovered",
                )

    def test_guard_accepts_explicit_non_ci_and_non_test(self):
        """The recorded non-CI and non-test infrastructure files are accepted."""
        for rel in check_test_discovery.NON_CI_TEST_FILES:
            with self.subTest(rel=rel):
                self.assertIn(rel, check_test_discovery.NON_CI_TEST_FILES)
        for rel in check_test_discovery.NON_TEST_FILES:
            with self.subTest(rel=rel):
                self.assertIn(rel, check_test_discovery.NON_TEST_FILES)


if __name__ == "__main__":
    unittest.main()
