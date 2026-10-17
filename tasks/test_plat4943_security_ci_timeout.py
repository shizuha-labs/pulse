import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo/workflows/security-ci.yml"


class SecurityCiPipCacheTimeoutTests(unittest.TestCase):
    def test_cold_fill_timeout_is_explicit_and_bounded(self):
        workflow = WORKFLOW.read_text()

        # pip defaults to a 15-second socket timeout, which is shorter than a
        # legitimate cold fill of Semgrep's large ARM64 wheel (PLAT-4943).
        self.assertIn("pip_cache_args=(", workflow)
        self.assertRegex(workflow, r"(?m)^\s+--timeout 120$")
        self.assertRegex(workflow, r"(?m)^\s+--retries 1$")

        install_prefix = 'python3 -m pip install "${pip_cache_args[@]}"'
        self.assertEqual(workflow.count(install_prefix), 2)
        self.assertIn(
            f"retry timeout -k 30 480 {install_prefix} "
            "'semgrep>=1.75,<2' 'bandit[toml]>=1.7,<2' 'uv==0.11.26'",
            workflow,
        )
        self.assertNotIn(
            "python3 -m pip install --index-url "
            "http://pip-cache.registry.svc.cluster.local/simple",
            workflow,
        )
