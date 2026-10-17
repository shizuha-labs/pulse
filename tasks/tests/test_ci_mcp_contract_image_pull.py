"""PLAT-5145: keep the MCP contract lane deterministic and diagnosable.

The in-cluster test Job used mutable Docker Hub tags and waited the full suite
timeout when a Pod remained Pending in ImagePullBackOff.  These structural
tests live under ``tasks/tests`` so the required premerge Django suite executes
them, rather than leaving the workflow contract in a root-level test directory
that the scoped gate does not discover.
"""

import subprocess
from pathlib import Path
from unittest import TestCase


WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".forgejo"
    / "workflows"
    / "mcp-contract-tests.yml"
)


class McpContractImagePullTests(TestCase):
    def test_job_images_are_multiarch_digest_pinned(self):
        workflow = WORKFLOW.read_text()

        self.assertIn(
            "image: docker.io/alpine/git:2.43.0@sha256:"
            "76fdb7210689fc26c6ff101c4adacf9d12d3d25a919a7d8ff42ebaef5bedba65",
            workflow,
        )
        self.assertIn(
            "image: docker.io/library/python:3.12-bookworm@sha256:"
            "9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a",
            workflow,
        )
        self.assertEqual(workflow.count("imagePullPolicy: IfNotPresent"), 2)
        self.assertNotIn("image: alpine/git:2.43.0\n", workflow)
        self.assertNotIn("image: python:3.12-bookworm\n", workflow)

    def test_pull_failure_classifier_accepts_only_image_wait_reasons(self):
        workflow = WORKFLOW.read_text()
        classifier = (
            '$2 == "ErrImagePull" || $2 == "ImagePullBackOff" '
            '|| $2 == "InvalidImageName" { print; exit }'
        )
        self.assertIn(classifier, workflow)

        def classify(waiting_states):
            result = subprocess.run(
                ["awk", "-F", "|", classifier],
                input=waiting_states,
                text=True,
                capture_output=True,
                check=True,
            )
            return result.stdout.strip()

        self.assertEqual(
            classify(
                "git-clone|PodInitializing|waiting for volume\n"
                "contract-tests|ImagePullBackOff|back-off pulling image\n"
            ),
            "contract-tests|ImagePullBackOff|back-off pulling image",
        )
        self.assertEqual(
            classify("git-clone|PodInitializing|waiting for volume\n"),
            "",
        )

    def test_pull_failure_is_described_before_trap_cleanup(self):
        workflow = WORKFLOW.read_text()
        monitor = workflow.split("          YAML\n", 1)[1]

        self.assertIn("trap cleanup EXIT", workflow)
        self.assertIn("CI_IMAGE_PULL_FAILED:", monitor)
        self.assertLess(
            monitor.index('kubectl describe pod -n build "${POD}"'),
            monitor.index('echo "MCP contract tests FAILED'),
        )
        # Once the Job exists, deletion is owned only by the EXIT trap. This
        # prevents the failure branch from erasing the Pod before describe.
        self.assertNotIn("kubectl delete job", monitor)
