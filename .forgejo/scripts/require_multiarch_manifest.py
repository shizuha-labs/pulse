"""Fail closed unless a registry manifest index has both required platforms."""

import json
import sys

REQUIRED = {"linux/amd64", "linux/arm64"}


def main() -> int:
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
    platforms = {
        f"{(item.get('platform') or {}).get('os')}/{(item.get('platform') or {}).get('architecture')}"
        for item in manifest.get("manifests", [])
    }
    missing = REQUIRED - platforms
    print(f"registry manifest ready platforms={sorted(platforms)}")
    if missing:
        print(f"registry manifest for desired tag is missing {sorted(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
