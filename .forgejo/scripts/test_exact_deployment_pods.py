from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("exact-deployment-pods.py")
SPEC = importlib.util.spec_from_file_location("exact_deployment_pods", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _pod(
    name: str,
    image: str,
    *,
    ready: bool,
    restarts: int = 0,
    deleting: bool = False,
    owner_kind: str = "ReplicaSet",
    phase: str = "Running",
) -> dict:
    metadata = {
        "name": name,
        "ownerReferences": [
            {"controller": True, "kind": owner_kind, "name": f"{name}-owner"}
        ],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-07-26T18:09:00Z"
    return {
        "metadata": metadata,
        "spec": {"containers": [{"name": "app", "image": image}]},
        "status": {
            "phase": phase,
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"}
            ],
            "containerStatuses": [
                {"name": "app", "ready": ready, "restartCount": restarts}
            ],
        },
    }


class ExactDeploymentPodsTests(unittest.TestCase):
    expected = "registry/service:new"

    def test_exact_ready_revision_is_reported(self) -> None:
        payload = {
            "items": [
                _pod("new-1", self.expected, ready=True),
                _pod("new-2", self.expected, ready=True),
            ]
        }
        self.assertEqual(MODULE.summarize(payload, self.expected), (2, 2, 0, 0, 0))

    def test_mixed_revision_cannot_masquerade_as_desired_ready_count(self) -> None:
        payload = {
            "items": [
                _pod("new-ready", self.expected, ready=True),
                _pod("new-unready", self.expected, ready=False),
                _pod("old-ready", "registry/service:old", ready=True),
            ]
        }
        self.assertEqual(MODULE.summarize(payload, self.expected), (2, 1, 1, 0, 0))

    def test_terminating_old_revision_does_not_hold_completed_rollout(self) -> None:
        payload = {
            "items": [
                _pod("new-1", self.expected, ready=True),
                _pod("new-2", self.expected, ready=True),
                _pod("old-deleting", "registry/service:old", ready=True, deleting=True),
            ]
        }
        self.assertEqual(MODULE.summarize(payload, self.expected), (2, 2, 0, 0, 0))

    def test_foreign_owner_and_restart_are_fail_closed_signals(self) -> None:
        payload = {
            "items": [
                _pod("new-1", self.expected, ready=True, restarts=1),
                _pod(
                    "manual-lookalike",
                    self.expected,
                    ready=True,
                    owner_kind="Job",
                ),
            ]
        }
        self.assertEqual(MODULE.summarize(payload, self.expected), (1, 1, 0, 1, 1))

    def test_completed_pods_do_not_hold_completed_rollout(self) -> None:
        # Run 8909: retained Failed old backend and Succeeded exact frontend
        # pods predated the release and made its convergence gate impossible.
        payload = {
            "items": [
                _pod("new-ready", self.expected, ready=True),
                _pod("old-failed", "registry/service:old", ready=False, phase="Failed"),
                _pod("new-completed", self.expected, ready=False, phase="Succeeded"),
            ]
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--expected-image", self.expected],
            input=json.dumps(payload), text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "1|1|0|0|0")

    def test_nonterminal_pods_continue_to_block_convergence(self) -> None:
        for phase in ("Pending", "Running", "Unknown", ""):
            with self.subTest(phase=phase):
                payload = {
                    "items": [
                        _pod("new-ready", self.expected, ready=True),
                        _pod("old-active", "registry/service:old", ready=False, phase=phase),
                        _pod("new-unready", self.expected, ready=False, phase=phase),
                    ]
                }
                self.assertEqual(MODULE.summarize(payload, self.expected), (2, 1, 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
