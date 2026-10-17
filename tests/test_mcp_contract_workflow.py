from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".forgejo" / "workflows" / "mcp-contract-tests.yml"
PREMERGE_WORKFLOW = ROOT / ".forgejo" / "workflows" / "premerge.yml"
DEPLOY_WORKFLOW = ROOT / ".forgejo" / "workflows" / "deploy.yml"


class McpContractWorkflowTests(unittest.TestCase):
    def test_contract_orchestration_keeps_the_deploy_lane_free(self):
        workflow = WORKFLOW.read_text()

        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: deploy-lane", workflow)

    def test_dependency_bootstrap_is_reproducible_cached_and_bounded(self):
        workflow = WORKFLOW.read_text()

        self.assertIn("activeDeadlineSeconds: 1500", workflow)
        self.assertIn("uv export --frozen --no-dev --no-emit-project", workflow)
        self.assertIn("UV_HTTP_TIMEOUT=60 UV_HTTP_RETRIES=10 UV_CONCURRENT_DOWNLOADS=4 uv pip sync", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("/tmp/ci-requirements.txt", workflow)
        self.assertNotIn("--exclude-newer-package setuptools=false", workflow)
        self.assertIn("--index-url http://pip-cache.registry.svc.cluster.local/simple", workflow)
        self.assertIn("--allow-insecure-host pip-cache.registry.svc.cluster.local", workflow)
        self.assertNotIn("UV_CONCURRENT_DOWNLOADS=4 uv sync", workflow)

    def test_full_suite_dependency_bootstraps_use_the_same_frozen_cache_route(self):
        for workflow_path in (PREMERGE_WORKFLOW, DEPLOY_WORKFLOW):
            with self.subTest(workflow=workflow_path.name):
                workflow = workflow_path.read_text()
                self.assertIn("uv export --frozen --no-dev --no-emit-project", workflow)
                self.assertIn(
                    "UV_HTTP_TIMEOUT=60 UV_HTTP_RETRIES=10 UV_CONCURRENT_DOWNLOADS=4 uv pip sync",
                    workflow,
                )
                self.assertIn("--require-hashes", workflow)
                self.assertIn("/tmp/ci-requirements.txt", workflow)
                self.assertNotIn("--exclude-newer-package setuptools=false", workflow)
                self.assertIn(
                    "--index-url http://pip-cache.registry.svc.cluster.local/simple",
                    workflow,
                )
                self.assertIn(
                    "--allow-insecure-host pip-cache.registry.svc.cluster.local",
                    workflow,
                )
                self.assertNotIn("UV_CONCURRENT_DOWNLOADS=4 uv sync", workflow)


if __name__ == "__main__":
    unittest.main()
