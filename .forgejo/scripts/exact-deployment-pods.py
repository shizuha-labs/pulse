#!/usr/bin/env python3
"""Summarize exact-image Deployment pods from a Kubernetes PodList.

The caller must already scope the PodList with the Deployment's exact label
selector.  Aggregate Deployment status can briefly report desired ready and
available counts while old ReplicaSet pods overlap unready new ReplicaSet pods.
This helper keeps release convergence tied to the requested image revision.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def summarize(payload: dict[str, Any], expected_image: str) -> tuple[int, int, int, int, int]:
    exact = 0
    ready = 0
    other = 0
    foreign = 0
    restarts = 0

    for pod in payload.get("items", []):
        metadata = pod.get("metadata") or {}
        if metadata.get("deletionTimestamp"):
            continue
        # Kubernetes can retain terminal pods after their replacements are
        # ready. They no longer participate in Deployment replica counts.
        if (pod.get("status") or {}).get("phase") in {"Succeeded", "Failed"}:
            continue

        owners = metadata.get("ownerReferences") or []
        replica_set_owned = any(
            owner.get("controller") is True and owner.get("kind") == "ReplicaSet"
            for owner in owners
        )
        if not replica_set_owned:
            foreign += 1
            continue

        images = {
            container.get("image")
            for container in (pod.get("spec") or {}).get("containers", [])
        }
        if expected_image not in images:
            other += 1
            continue

        exact += 1
        conditions = (pod.get("status") or {}).get("conditions") or []
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in conditions
        ):
            ready += 1
        restarts += sum(
            int(status.get("restartCount") or 0)
            for status in (pod.get("status") or {}).get("containerStatuses", [])
        )

    return exact, ready, other, foreign, restarts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-image", required=True)
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    print("|".join(str(value) for value in summarize(payload, args.expected_image)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
