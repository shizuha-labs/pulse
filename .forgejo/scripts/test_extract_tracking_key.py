"""PLAT-10373 (rei review 5633): focused tests for extract_tracking_key.py.

Covers the three-way contract the review demanded:
  0 = verified key (case-insensitive match, normalized uppercase, primary pick)
  3 = lookup/parse/provenance error (fail closed — caller must NOT pin)
  4 = verifiably keyless message (caller MAY use its carrier fallback)
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract_tracking_key.py"





def _module():
    spec = importlib.util.spec_from_file_location("extract_tracking_key", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["extract_tracking_key"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- primary_key unit behavior -------------------------------------------------

def test_uppercase_key_normalized_unchanged():
    assert _module().primary_key("Merge PR 'x (PLS-1303 leg 4)' (#669)") == "PLS-1303"


def test_lowercase_key_normalized_to_uppercase():
    assert _module().primary_key("test(pls-1349): pin correlation") == "PLS-1349"


def test_first_match_wins_and_is_normalized():
    assert _module().primary_key("chore(pls-99): follow-up for PLAT-10373 and scli-812") == "PLS-99"


def test_slug_without_hyphen_digit_boundary_is_not_a_key():
    # 'pls1349-merge-correlation-pin' has no KEY-123 boundary; must not match.
    assert _module().primary_key(
        "Merge pull request 'x' (#669) from pls1349-merge-correlation-pin into main"
    ) is None


def test_mixed_case_key_normalized():
    assert _module().primary_key("fix(Plat-7): thing") == "PLAT-7"


# --- CLI contract --------------------------------------------------------------

def test_exit0_verified_key_from_real_commit_envelope():
    # The 996521e shape: lowercase primary in the title prefix (pls-1349) plus an
    # uppercase mention later (PLS-1303). The PRIMARY is the first key, normalized.
    doc = {"sha": "996521e",
           "commit": {"message": "Merge PR 'test(pls-1349): pin (PLS-1303 leg 4)' (#669)"}}
    code, out = _run(json.dumps(doc))
    assert code == 0 and out == "PLS-1349"


def test_exit3_unparsable_json_fails_closed():
    code, out = _run("<html>gateway error</html>")
    assert code == 3 and out == ""
    err = subprocess.run(
        [sys.executable, str(SCRIPT), "--expect-sha", "996521e"],
        input="<html/>", capture_output=True, text=True,
    ).stderr
    assert "provenance error" in err


def test_exit3_error_envelope_fails_closed():
    # Forgejo error envelope: no commit/message shape — must NOT become the carrier.
    doc = {"message": "The target couldn't be found.", "url": "https://origin.shizuha.com/api/swagger"}
    code, out = _run(json.dumps(doc))
    assert code == 3 and out == ""


def test_exit3_empty_message_fails_closed():
    doc = {"sha": "996521e", "commit": {"message": ""}}
    code, out = _run(json.dumps(doc))
    assert code == 3 and out == ""


def test_exit4_verifiably_keyless_message():
    doc = {"sha": "996521e", "commit": {"message": "chore: rotate mirror digests only"}}
    code, out = _run(json.dumps(doc), sha="996521e")
    assert code == 4 and out == ""


# --- rei round-4 P2: SHA provenance controls ---------------------------------

def test_exit3_sha_mismatch_fails_closed():
    """A body identifying a DIFFERENT commit than the pinned source must fail
    closed — provenance identifies the pinned source, not just a message."""
    doc = {"sha": "deadbeef", "commit": {"message": "fix(pls-1349): unrelated commit with a key"}}
    code, out = _run(json.dumps(doc), sha="996521e")
    assert code == 3 and out == ""
    err = subprocess.run(
        [sys.executable, str(SCRIPT), "--expect-sha", "996521e"],
        input=json.dumps(doc), capture_output=True, text=True,
    ).stderr
    assert "does not identify the pinned" in err


def test_exit3_error_envelope_lacks_sha_fails_closed():
    doc = {"message": "The target couldn't be found.", "url": "https://origin.shizuha.com/api/swagger"}
    code, out = _run(json.dumps(doc), sha="996521e")
    assert code == 3 and out == ""


def _run(payload: str, sha: str = "996521e") -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--expect-sha", sha],
        input=payload, capture_output=True, text=True,
    )
    return r.returncode, r.stdout.strip()


# --- rui round-5 P1: tag suffix is 7 chars, live API returns the full 40 ---

FULL_SHA = "996521e" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

def test_exit0_full_api_sha_with_abbreviated_tag_suffix():
    """The production shape: PIN_SOURCE_SHA = 7-char tag suffix, doc.sha = full
    40-char sha from the live git/commits API. Prefix compare must verify."""
    doc = {"sha": FULL_SHA,
           "commit": {"message": "Merge PR 'test(pls-1349): pin (PLS-1303 leg 4)' (#669)"}}
    code, out = _run(json.dumps(doc))
    assert code == 0 and out == "PLS-1349"


def test_exit0_full_sha_case_insensitive_prefix():
    doc = {"sha": FULL_SHA.upper(),
           "commit": {"message": "fix(pls-1349): pin correlation"}}
    code, out = _run(json.dumps(doc))
    assert code == 0 and out == "PLS-1349"


def test_exit3_full_sha_different_prefix_fails_closed():
    doc = {"sha": "deadbee" + FULL_SHA[7:],
           "commit": {"message": "fix(pls-1349): unrelated commit with a key"}}
    code, out = _run(json.dumps(doc))
    assert code == 3 and out == ""


def test_exit3_abbreviated_expect_below_7_chars_fails_closed():
    """A too-short --expect-sha must never pass provenance even if the doc sha
    starts with it — prefix identity needs the >=7-hex anchor."""
    doc = {"sha": FULL_SHA, "commit": {"message": "fix(pls-1349): pin"}}
    code, out = _run(json.dumps(doc), sha="996521")
    assert code == 3 and out == ""
