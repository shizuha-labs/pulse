#!/usr/bin/env python3
"""Pick a sha256 image digest from a pod list (PLS-1346).

Reads `kubectl get pods -o json` on stdin. Prints one `sha256:<64 hex>`
line, or nothing. Non-sha256 imageIDs are dropped. A container matches
when its name equals COMPONENT or its image basename starts with the
IMAGE repo name.
"""
import json
import os
import re
import sys

def select_digest(pods, *, component: str, image: str) -> str:
    image_name = image.rsplit("/", 1)[-1].split(":")[0]
    for pod in pods or []:
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            image_id = cs.get("imageID") or ""
            match = re.search(r"sha256:[0-9a-f]{64}", image_id)
            if not match:
                continue
            cs_image = (cs.get("image") or "").rsplit("/", 1)[-1]
            if cs.get("name") == component or (
                image_name and cs_image.startswith(image_name + ":")
            ):
                return match.group(0)
    return ""


def main() -> int:
    component = os.environ.get("COMPONENT", "")
    image = os.environ.get("IMAGE", "")
    try:
        pods = json.load(sys.stdin).get("items", [])
    except Exception:
        pods = []
    digest = select_digest(pods, component=component, image=image)
    if digest:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
