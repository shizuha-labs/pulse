"""PLAT-10373 (rei review round-4, P1): clean-workspace materialization
integration for BOTH deploy lanes.

The deploy/verify steps invoke ``python3 .forgejo/scripts/<tool>.py`` inside a
workspace assembled ONLY by the lane's explicit ``install -m 0644`` list. A
tool that is invoked but never installed reproduces as ``python3: can't open
file ... exit 2`` in a clean workspace — the exact wedge rei reproduced.

This integration test parses each lane workflow and asserts every invoked
``.forgejo/scripts/*.py`` is materialized by a matching install line —
executable, so adding an invocation without installing it fails here first.
Mutant check: removing the extractor's install line must fail this test.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANES = [
    ROOT / ".forgejo" / "workflows" / "deploy.yml",
    ROOT / ".forgejo" / "workflows" / "deploy-frontend.yml",
]

INSTALL_RE = re.compile(
    r'install -m 0644 "\$\{SRC_DIR\}/(\.forgejo/scripts/[A-Za-z0-9_./-]+\.py)"'
)
INVOKE_RE = re.compile(r'python3 (\.[A-Za-z0-9_./-]*scripts/[A-Za-z0-9_./-]+\.py)')


class CleanWorkspaceMaterializationTests(unittest.TestCase):
    def test_both_lanes_install_every_invoked_script(self):
        for lane in LANES:
            text = lane.read_text()
            installed = set(INSTALL_RE.findall(text))
            invoked = set(INVOKE_RE.findall(text))
            missing = sorted(invoked - installed)
            self.assertEqual(
                missing, [],
                f"{lane.name}: scripts invoked but never installed — they exit 2 "
                f"in a clean workspace (rei round-4 P1). Add install lines for: {missing}",
            )
            self.assertIn(".forgejo/scripts/extract_tracking_key.py", installed,
                          f"{lane.name}: the tracking-key extractor must be materialized")

    def test_extractor_is_invoked_with_provenance_sha(self):
        """P2: the invocation must bind the pinned source SHA (--expect-sha)."""
        for lane in LANES:
            text = lane.read_text()
            self.assertRegex(
                text,
                r"extract_tracking_key\.py --expect-sha \"\$\{PIN_SOURCE_SHA\}\"",
                f"{lane.name}: extractor invocation must carry --expect-sha",
            )
            # the fetch must be bounded to HTTP 200 (no tolerated error envelopes)
            self.assertNotIn(
                "--accept-codes '^(200|201|404)$' GET", text,
                f"{lane.name}: the source-commit fetch must not tolerate 404s — "
                "provenance unobtainable must fail closed, not fall through",
            )


if __name__ == "__main__":
    unittest.main()
