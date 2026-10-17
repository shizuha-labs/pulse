from pathlib import Path
from unittest import TestCase, main


class OriginKubectlPackageContractTests(TestCase):
    def test_every_kubectl_bootstrap_uses_pinned_bounded_origin_package(self):
        workflows = Path(__file__).parents[2] / ".forgejo/workflows"
        expected_url = (
            "http://forgejo-http.origin.svc.cluster.local/api/packages/actions/"
            "generic/kubectl/v1.34.1/kubectl-${KUBECTL_ARCH}"
        )
        expected_hashes = {
            "amd64": "7721f265e18709862655affba5343e85e1980639395d5754473dafaadcaa69e3",
            "arm64": "420e6110e3ba7ee5a3927b5af868d18df17aae36b720529ffa4e9e945aa95450",
        }
        checked = []

        for path in workflows.glob("*.yml"):
            text = path.read_text()
            if "-o kubectl" not in text:
                continue
            checked.append(path.name)
            self.assertNotIn("dl.k8s.io/release", text, path.name)
            self.assertIn(expected_url, text, path.name)
            self.assertNotIn('-H "Authorization: token ${{ secrets.CI_PACKAGE_TOKEN }}"', text, path.name)
            self.assertIn("anonymous-fetch: sanctioned", text, path.name)
            for digest in expected_hashes.values():
                self.assertIn(digest, text, path.name)
            for required in (
                "curl --retry",
                "--retry-all-errors",
                "--max-time",
                "sha256sum -c",
            ):
                self.assertIn(required, text, f"{path.name}: missing {required}")

        self.assertEqual(
            sorted(checked),
            sorted(["deploy-frontend.yml", "mcp-contract-tests.yml", "premerge.yml"]),
        )


    def test_deploy_uses_verified_vendored_clients_without_network_bootstrap(self):
        import yaml
        path = Path(__file__).parents[2] / ".forgejo/workflows/deploy.yml"
        source = path.read_text()
        workflow = yaml.safe_load(source)
        expected_image = (
            "registry.registry.svc.cluster.local:5000/origin-ci-helm@sha256:"
            "90f02d48d68c0ed690f9ddd8bcef1167490610362c19d429943931433ed7b194"
        )
        self.assertNotIn("-o kubectl", source)
        self.assertNotIn("dl.k8s.io/release", source)
        self.assertEqual(set(workflow["jobs"]), {"test", "build", "deploy"})
        for name, job in workflow["jobs"].items():
            self.assertEqual(job["container"]["image"], expected_image, name)
            bootstrap = next(step["run"] for step in job["steps"]
                             if "KUBE_CONFIG_B64" in step.get("run", ""))
            self.assertIn("kubectl version --client -o json", bootstrap, name)
            self.assertIn('== "v1.34.6"', bootstrap, name)
            self.assertIn("helm version --short | grep -F 'v3.21.2'", bootstrap, name)


if __name__ == "__main__":
    main()
