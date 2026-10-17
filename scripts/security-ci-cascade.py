#!/usr/bin/env python3
"""Open or update normal Origin PRs carrying the canonical security-ci bundle."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR_PATH = ROOT / "scripts/security-ci-vendor.py"


def load_vendor():
    spec = importlib.util.spec_from_file_location("security_ci_vendor", VENDOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load security-ci vendor module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Forgejo:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        url = f"{self.base_url}/api/v1{path}"
        # urllib refuses to follow redirects for non-GET/HEAD methods (a repo
        # rename makes Forgejo 301 the old path and the whole fleet cascade
        # dies mid-run). Re-issue PUT/POST to the redirect target, preserving
        # method and payload (PLAT-5442 follow-up, 2026-09-26).
        for _ in range(5):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"token {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 307, 308) and exc.headers.get("Location"):
                    url = urllib.parse.urljoin(url, exc.headers["Location"])
                    continue
                body = exc.read().decode(errors="replace")
                raise RuntimeError(f"Forgejo {method} {path} failed: HTTP {exc.code}: {body[:500]}") from exc

    def optional(self, method: str, path: str):
        try:
            return self.request(method, path)
        except RuntimeError as exc:
            if "HTTP 404:" in str(exc):
                return None
            raise


def ensure_branch(client: Forgejo, owner: str, repo: str, branch: str, base: str) -> None:
    encoded = urllib.parse.quote(branch, safe="")
    if client.optional("GET", f"/repos/{owner}/{repo}/branches/{encoded}") is not None:
        return
    client.request(
        "POST",
        f"/repos/{owner}/{repo}/branches",
        {"new_branch_name": branch, "old_branch_name": base},
    )


def bundle_matches_ref(
    client: Forgejo,
    owner: str,
    repo: str,
    ref: str,
    bundle: dict[str, bytes],
) -> bool:
    """Return whether every generated file already matches ``ref`` exactly."""
    encoded_ref = urllib.parse.quote(ref, safe="")
    for path, data in bundle.items():
        encoded_path = urllib.parse.quote(path, safe="/")
        current = client.optional(
            "GET",
            f"/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_ref}",
        )
        if current is None or base64.b64decode(current["content"]) != data:
            return False
    return True


def put_file(
    client: Forgejo,
    owner: str,
    repo: str,
    branch: str,
    path: str,
    data: bytes,
    message: str,
) -> bool:
    encoded_path = urllib.parse.quote(path, safe="/")
    encoded_branch = urllib.parse.quote(branch, safe="")
    current = client.optional(
        "GET",
        f"/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_branch}",
    )
    if current is not None and base64.b64decode(current["content"]) == data:
        return False
    payload = {
        "branch": branch,
        "content": base64.b64encode(data).decode(),
        "message": message,
    }
    if current is not None:
        payload["sha"] = current["sha"]
    method = "PUT" if current is not None else "POST"
    client.request(method, f"/repos/{owner}/{repo}/contents/{encoded_path}", payload)
    return True


def open_or_find_pr(
    client: Forgejo,
    owner: str,
    repo: str,
    branch: str,
    base: str,
    version: int,
) -> str:
    page = 1
    while True:
        pulls = client.request(
            "GET",
            f"/repos/{owner}/{repo}/pulls?state=open&limit=50&page={page}",
        )
        for pull in pulls:
            if pull["head"]["ref"] == branch and pull["base"]["ref"] == base:
                return pull["html_url"]
        if len(pulls) < 50:
            break
        page += 1
    pull = client.request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        {
            "title": f"PLAT-5093: sync security-ci contract v{version}",
            "head": branch,
            "base": base,
            "body": (
                "Pulse: PLAT-5093\n\n"
                "Generated from the reviewed shizuha-tasks security-ci template. "
                "Carries the bounded pip-cache contract, exact-byte parity gate, "
                "and canonical normalizer. Do not hand-edit generated files."
            ),
        },
    )
    return pull["html_url"]


def sync_repo(
    client: Forgejo,
    vendor,
    manifest: dict,
    row: dict,
    owner: str,
    branch: str,
    version: int,
) -> tuple[bool, str | None]:
    """Sync one consumer, avoiding branch writes and PRs when base is current."""
    repo = row["repo"]
    base = row["default_branch"]
    meta = client.request("GET", f"/repos/{owner}/{repo}")
    if meta["default_branch"] != base:
        raise RuntimeError(
            f"{repo}: manifest branch {base} != live default {meta['default_branch']}"
        )

    bundle = vendor.render_bundle(manifest, repo)
    if bundle_matches_ref(client, owner, repo, base, bundle):
        return False, None

    ensure_branch(client, owner, repo, branch, base)
    changed = False
    for path, data in bundle.items():
        changed |= put_file(
            client,
            owner,
            repo,
            branch,
            path,
            data,
            f"PLAT-5093: sync {path}",
        )
    pr = open_or_find_pr(client, owner, repo, branch, base, version)
    return changed, pr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", default="shizuha-labs")
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    vendor = load_vendor()
    manifest = vendor.load_manifest()
    errors = vendor.verify(manifest)
    if errors:
        for error in errors:
            print(f"security-ci inventory error: {error}")
        return 1

    selected = set(args.repos or [])
    rows = [
        row
        for row in manifest["consumers"]
        if row["repo"] != manifest["source"]["repo"]
        and (not selected or row["repo"] in selected)
    ]
    if selected - {row["repo"] for row in rows}:
        print(f"unknown/non-target repos: {sorted(selected - {row['repo'] for row in rows})}")
        return 2

    version = manifest["contract_version"]
    branch = f"automation/plat-5093-security-ci-v{version}"
    if not args.apply:
        for row in rows:
            bundle = vendor.render_bundle(manifest, row["repo"])
            print(f"DRY-RUN {row['repo']} {row['default_branch']} {vendor.bundle_sha256(bundle)}")
        return 0

    token = os.environ.get("SECURITY_CI_CASCADE_TOKEN")
    if not token:
        print("SECURITY_CI_CASCADE_TOKEN is required for --apply")
        return 2
    client = Forgejo(os.environ.get("FORGEJO_URL", "https://origin.shizuha.com"), token)
    for row in rows:
        repo = row["repo"]
        changed, pr = sync_repo(
            client,
            vendor,
            manifest,
            row,
            args.owner,
            branch,
            version,
        )
        if pr is None:
            print(f"{repo}: already current; no branch write or PR")
        else:
            print(f"{repo}: {'updated' if changed else 'unchanged'} {pr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
