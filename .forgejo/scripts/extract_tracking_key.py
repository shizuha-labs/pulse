#!/usr/bin/env python3
"""PLAT-10373 (rei review 5633): provenance-checked tracking-key extraction.

Reads the pinned source commit's Forgejo API JSON body (on stdin or --file) and
prints the normalized primary Pulse tracking key for the tag-pin commit message.

Contract:
  exit 0 + key      — the fetched body IS the pinned source commit (its top-level
                      ``sha`` case-insensitively STARTS WITH --expect-sha, which
                      must be >=7 hex chars) AND its message contains at least
                      one case-insensitive KEY-123 token; the PRIMARY key
                      (normalized to uppercase) is printed.
                      (PLAT-10373 rui round-5 P1: the tag-pin lanes derive
                      PIN_SOURCE_SHA from the tag suffix, which is the 7-char
                      abbreviated sha, while the live git/commits API returns
                      the full 40-char sha — exact equality made every real
                      fetch exit 3. Prefix compare keeps provenance anchored to
                      the pinned source while tolerating abbreviation.)
  exit 3            — lookup/parse/provenance error: unparsable JSON, error
                      envelope, missing/empty message, or **sha mismatch with
                      --expect-sha** (provenance must identify the pinned
                      source, not just any commit with a message). The caller
                      MUST fail the pin loudly; silently substituting the
                      carrier key here would mask a provenance failure.
  exit 4            — fetched+verified source commit whose message verifiably
                      contains NO key token. The caller may use its carrier
                      fallback.
"""
import argparse
import json
import re
import sys

KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)-(\d+)\b")


def primary_key(message: str) -> str | None:
    """First case-insensitive KEY-123 token, normalized to uppercase.

    Branch names inside merge messages (e.g. 'from pls1349-merge-correlation-pin')
    do not match KEY_RE (no hyphen-digit boundary in the slug), so the first
    match is a genuine task key reference in the subject/body, not a slug
    fragment. Normalization makes lowercase refs (pls-1349) and uppercase refs
    (PLS-1303) comparable.
    """
    m = KEY_RE.search(message or "")
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="-")
    ap.add_argument("--expect-sha", required=True,
                    help="the pinned source SHA the fetched commit must identify")
    args = ap.parse_args()
    raw = open(args.file).read() if args.file != "-" else sys.stdin.read()

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        print("PLAT-10373 provenance error: source-commit fetch body is not JSON", file=sys.stderr)
        return 3

    # Provenance: the Forgejo contents API returns the commit object on 200 and
    # an error envelope otherwise. Require the shape of a real commit whose
    # sha case-insensitively starts with --expect-sha (>=7 hex chars): the tag
    # suffix is the abbreviated sha of the pinned source while the API returns
    # the full sha (rui round-5 P1). Anything else is a lookup/provenance
    # failure, not "no key".
    expected = (args.expect_sha or "").strip().lower()
    doc_sha = str(doc.get("sha") or "").strip().lower() if isinstance(doc, dict) else ""
    if (
        not isinstance(doc, dict)
        or len(expected) < 7
        or not doc_sha
        or not doc_sha.startswith(expected)
    ):
        print(
            "PLAT-10373 provenance error: fetched body does not identify the pinned "
            f"source sha {args.expect_sha} (error envelope, wrong commit, or stale body)",
            file=sys.stderr,
        )
        return 3
    commit = doc.get("commit") if isinstance(doc, dict) else None
    message = (commit or {}).get("message")
    if not isinstance(message, str) or not message.strip():
        print("PLAT-10373 provenance error: source-commit lookup did not return a parsable commit message", file=sys.stderr)
        return 3

    key = primary_key(message)
    if key is None:
        print("PLAT-10373: source commit message verifiably contains no tracking key", file=sys.stderr)
        return 4
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
