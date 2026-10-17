import pytest
import importlib.util
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "security-ci-to-pulse.py"
FIX = ROOT / "tests" / "fixtures" / "security_ci"


def run_script(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def load_module():
    spec = importlib.util.spec_from_file_location("security_ci_to_pulse", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_security_ci_parser_dry_run_reports_and_fails_on_high(tmp_path):
    summary = tmp_path / "summary.md"
    proc = run_script(
        "--semgrep", str(FIX / "semgrep.json"),
        "--bandit", str(FIX / "bandit.json"),
        "--osv", str(FIX / "osv.json"),
        "--trivy", str(FIX / "trivy.json"),
        "--repo", "shizuha-labs/example",
        "--sha", "abc123",
        "--dry-run",
        "--fail-on", "high",
        "--summary", str(summary),
    )
    assert proc.returncode == 1
    out = proc.stdout + proc.stderr
    assert "security-ci: 3 active finding(s), 0 suppressed" in out
    assert "bandit" in out
    assert "semgrep" in out
    assert "deps" in out
    assert "pkg:shizuha-labs/example:na:demo" in out
    assert "failing because" in out
    assert "Active findings: **3**" in summary.read_text()


def test_security_ci_parser_passes_when_threshold_above_findings(tmp_path):
    proc = run_script(
        "--bandit", str(FIX / "bandit.json"),
        "--dry-run",
        "--fail-on", "critical",
        "--summary", str(tmp_path / "summary.md"),
    )
    assert proc.returncode == 0
    assert "security-ci: 1 active finding(s)" in proc.stdout


def test_allowlist_suppresses_by_tool_rule_and_path(tmp_path):
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allowlist":[{
        "tool":"bandit","rule":"B602","path_contains":"app.py",
        "disposition":"accepted-false-positive",
        "reason":"fixture false positive","owner":"sec",
        "approved_by":"security@shizuha.com","expires":"2099-12-31",
    }]}))
    proc = run_script(
        "--bandit", str(FIX / "bandit.json"),
        "--dry-run",
        "--fail-on", "low",
        "--allowlist", str(allowlist),
        "--summary", str(tmp_path / "summary.md"),
    )
    assert proc.returncode == 0
    assert "0 active finding(s), 1 suppressed" in proc.stdout
    assert "fixture false positive" in proc.stdout


def test_allowlist_preserves_semgrep_raw_finding_suppression(tmp_path):
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({
        "allowlist": [{
            "tool": "semgrep",
            "rule": "python.lang.security.audit.dangerous-subprocess-use",
            "path_contains": "app.py",
            "disposition": "accepted-false-positive",
            "reason": "fixture false positive",
            "owner": "sec",
            "approved_by": "security@shizuha.com",
            "expires": "2099-12-31",
        }],
    }))
    proc = run_script(
        "--semgrep", str(FIX / "semgrep.json"),
        "--dry-run",
        "--fail-on", "low",
        "--allowlist", str(allowlist),
        "--summary", str(tmp_path / "summary.md"),
    )
    assert proc.returncode == 0
    assert "0 active finding(s), 1 suppressed" in proc.stdout
    assert "fixture false positive" in proc.stdout


def test_consolidated_dependency_allowlist_matches_final_identity(tmp_path):
    """PLAT-5289: apply PR #29's exact suppression after N advisories mint one package identity."""
    osv = tmp_path / "osv.json"
    osv.write_text(json.dumps({
        "results": [{
            "source": {"path": "uv.lock"},
            "packages": [
                {
                    "package": {"name": "python-multipart", "ecosystem": "PyPI"},
                    "vulnerabilities": [
                        {
                            "id": "GHSA-aaaa-1111",
                            "summary": "fixture A",
                            "severity": [{"type": "CVSS_V3", "score": "8.0"}],
                        },
                        {
                            "id": "GHSA-bbbb-2222",
                            "summary": "fixture B",
                            "severity": [{"type": "CVSS_V3", "score": "8.1"}],
                        },
                    ],
                },
                {
                    "package": {"name": "PyJWT", "ecosystem": "PyPI"},
                    "vulnerabilities": [{
                        "id": "GHSA-cccc-3333",
                        "summary": "negative control",
                        "severity": [{"type": "CVSS_V3", "score": "8.0"}],
                    }],
                },
            ],
        }],
    }))

    proc = run_script(
        "--osv", str(osv),
        "--repo", "shizuha-labs/shizuha-tasks",
        "--allowlist", str(ROOT / ".security-ci-allowlist.json"),
        "--dry-run",
        "--fail-on", "critical",
        "--summary", str(tmp_path / "summary.md"),
    )

    assert proc.returncode == 0
    assert "security-ci: 1 active finding(s), 1 suppressed" in proc.stdout
    assert "security-ci:2f8620abde3158ee5d453ee5" in proc.stdout
    assert "pkg:shizuha-labs/shizuha-tasks:PyPI:PyJWT" in proc.stdout
    assert "pkg:shizuha-labs/shizuha-tasks:PyPI:python-multipart" not in "\n".join(
        line for line in proc.stdout.splitlines() if line.startswith("- ")
    )


def test_non_high_dependency_allowlist_is_not_severity_gated(tmp_path):
    """PLAT-5289: medium dependencies normalize and suppress before the high filing threshold."""
    osv = tmp_path / "osv.json"
    osv.write_text(json.dumps({
        "results": [{
            "source": {"path": "uv.lock"},
            "packages": [{
                "package": {"name": "medium-dep", "ecosystem": "PyPI"},
                "vulnerabilities": [{
                    "id": "GHSA-medium-0001",
                    "summary": "medium fixture",
                    "severity": [{"type": "CVSS_V3", "score": "5.0"}],
                }],
            }],
        }],
    }))
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({
        "allowlist": [{
            "tool": "deps",
            "rule": "pkg:shizuha-labs/shizuha-tasks:PyPI:medium-dep",
            "path_contains": "deps/PyPI/medium-dep",
            "disposition": "accepted-false-positive",
            "reason": "non-high fixture suppression",
            "owner": "security",
            "approved_by": "security@shizuha.com",
            "expires": "2099-12-31",
        }],
    }))

    proc = run_script(
        "--osv", str(osv),
        "--repo", "shizuha-labs/shizuha-tasks",
        "--allowlist", str(allowlist),
        "--dry-run",
        "--fail-on", "high",
        "--summary", str(tmp_path / "summary.md"),
    )

    assert proc.returncode == 0
    assert "security-ci: 0 active finding(s), 1 suppressed" in proc.stdout
    assert "non-high fixture suppression" in proc.stdout


def test_source_id_allowlist_is_consumed_on_consecutive_scans(tmp_path):
    """PLAT-4772: a stable accepted source ID stays suppressed next scan."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allowlist":[{
        "source_id": finding.source_id,
        "disposition": "accepted-false-positive",
        "reason": "accepted false positive",
        "owner": "security",
        "approved_by": "security@shizuha.com",
        "expires": "2099-12-31",
    }]}))
    for run in ("first", "next"):
        proc = run_script(
            "--bandit", str(FIX / "bandit.json"),
            "--dry-run",
            "--fail-on", "low",
            "--allowlist", str(allowlist),
            "--summary", str(tmp_path / f"{run}.md"),
        )
        assert proc.returncode == 0
        assert "0 active finding(s), 1 suppressed" in proc.stdout
        assert finding.source_id in proc.stdout


def _list_serializer_row(
    source_id, *, item_key="SEC-1", status="in_progress",
    status_category="in_progress", severity=None,
):
    """A GET /items/ row shaped like Pulse's real ItemListSerializer.

    PLAT-2688: the list serializer exposes NEITHER `description` NOR `source_id`,
    only lightweight fields such as item_key/status/status_category. The prior
    dedupe check inspected description/source_id on the row and so never matched
    in production. Tests must use this realistic shape, not a row that happens to
    carry `description`.
    """
    row = {"id": "uuid-1", "item_key": item_key, "status": status, "status_category": status_category}
    if severity is not None:
        row["severity"] = severity
    return row


def _detail_payload(
    source_id, *, item_key="SEC-1", status="in_progress", status_category="in_progress",
    severity=None, labels=None, created_at=None, ident="uuid-1",
):
    """A GET /items/<id>/ payload shaped like Pulse's detail serializer.

    PLAT-5442: the detail endpoint (unlike the list serializer) DOES expose
    ``source_id`` — the positive-confirmation guard reads it here. Callers of
    ``pulse_find_existing`` receive this shape, so it carries everything the
    dedupe/PATCH paths need (labels, severity, status, status_category).
    """
    row = {
        "id": ident,
        "item_key": item_key,
        "source_id": source_id,
        "status": status,
        "status_category": status_category,
        "labels": labels or [],
    }
    if severity is not None:
        row["severity"] = severity
    if created_at is not None:
        row["created_at"] = created_at
    return row


def test_existing_pulse_finding_skips_unchanged_without_duplicate_or_comment(monkeypatch):
    module = load_module()
    requests = []

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        if method == "GET":
            if "/items/?" in url:
                # Realistic list payload: no description, no source_id (PLAT-2688).
                return {"results": [_list_serializer_row(finding.source_id)]}
            # Detail payload: positive-confirmation guard reads source_id here.
            return _detail_payload(finding.source_id)
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = type("Args", (), {"repo": "shizuha-labs/example", "ref": "refs/pull/1", "sha": "abc", "run_url": "http://run", "project_id": ""})()
    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)
    assert result == "skipped-unchanged:SEC-1"
    # An unchanged open finding creates neither activity spam nor a duplicate.
    assert not any(req[0] == "POST" and req[1].endswith("/comments/") for req in requests)
    assert not any(req[0] == "POST" and req[1].endswith("/items/") for req in requests)
    # The lookup constrained the query server-side by the exact source_id so the
    # match does not depend on per-row description/source_id fields.
    get_reqs = [req for req in requests if req[0] == "GET"]
    assert get_reqs and ("source_id=" in get_reqs[0][1])
    assert "search=" not in get_reqs[0][1]


def test_ledger_lookup_does_not_and_search_with_source_id(monkeypatch):
    """Ledger titles do not contain ``security-ci:<repo>:ledger``.

    AND-combining search= with source_id= returned zero rows and reminted a
    new ledger every weekly scan (live 2026-08-29, six open cortex ledgers).
    """
    module = load_module()
    requests = []

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        if method == "GET":
            if "/items/?" in url:
                # Realistic list payload: no source_id (PLAT-2688).
                assert "source_id=" in url
                assert "search=" not in url
                return {"results": [_list_serializer_row(
                    "security-ci:shizuha-labs/deploy:ledger", item_key="PLAT-5840",
                )]}
            # Detail payload: PLAT-5442 positive-confirmation guard reads source_id here.
            return _detail_payload(
                "security-ci:shizuha-labs/deploy:ledger", item_key="PLAT-5840",
            )
        if method == "PATCH":
            return {"item_key": "PLAT-5840"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [finding], _ledger_args(),
    )
    assert result.startswith("ledger-updated:PLAT-5840") or result.startswith("ledger-unchanged:PLAT-5840")
    get_reqs = [req for req in requests if req[0] == "GET"]
    assert get_reqs and "search=" not in get_reqs[0][1]


def test_repeated_run_new_sha_does_not_duplicate(monkeypatch):
    """AC regression: rerunning the SAME finding under a NEW run/SHA must dedupe.

    This is the exact production failure (PLAT-2688) — repeated security-ci runs
    on new commits refiled duplicate findings. The existing item is returned via
    the realistic list-serializer shape (no description/source_id); the script
    must skip without a comment and must NOT create a second item.
    """
    module = load_module()
    finding = next(module.parse_osv(module.load_json(str(FIX / "osv.json"))))
    posts_items, posts_comments = [], []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            if "/items/?" in url:
                return {"results": [_list_serializer_row(finding.source_id, item_key="PLAT-1372")]}
            return _detail_payload(finding.source_id, item_key="PLAT-1372")
        if method == "POST" and url.endswith("/comments/"):
            posts_comments.append(body)
            return {"id": "c1"}
        if method == "POST" and url.endswith("/items/"):
            posts_items.append(body)
            return {"item_key": "PLAT-DUP"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    # Second run: brand-new run/SHA, same finding payload.
    args = type("Args", (), {"repo": "shizuha-labs/example", "ref": "refs/heads/main", "sha": "deadbeef99", "run_url": "http://run/2", "project_id": "333"})()
    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)
    assert result == "skipped-unchanged:PLAT-1372"
    assert posts_items == []          # no duplicate task created
    assert posts_comments == []       # no per-run "observed again" spam


def test_existing_pulse_finding_comments_only_on_severity_change(monkeypatch):
    """A material severity shift still produces the one allowed update comment."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    posts_items, posts_comments = [], []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            if "/items/?" in url:
                return {"results": [_list_serializer_row(
                    finding.source_id,
                    item_key="SEC-2",
                    severity="warning",
                )]}
            return _detail_payload(finding.source_id, item_key="SEC-2", severity="warning")
        if method == "POST" and url.endswith("/comments/"):
            posts_comments.append(body)
            return {"id": "comment-1"}
        if method == "POST" and url.endswith("/items/"):
            posts_items.append(body)
            return {"item_key": "SEC-DUP"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    args = type("Args", (), {
        "repo": "shizuha-labs/example",
        "ref": "refs/heads/main",
        "sha": "abc123",
        "run_url": "http://run/3",
        "project_id": "333",
    })()

    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)

    assert result == "updated:SEC-2"
    assert posts_items == []
    assert len(posts_comments) == 1
    assert posts_comments[0]["item"] == "SEC-2"
    assert "Severity changed: `warning` → `error`." in posts_comments[0]["content"]


def test_terminal_finding_is_not_recommented(monkeypatch):
    """A finding already terminally dispositioned (rejected/fixed) is left alone.

    Satisfies the AC that dedupe covers terminal false-positive dispositions:
    no duplicate task AND no repeated comment noise on a closed finding.
    """
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    calls = []

    def fake_request(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET":
            if "/items/?" in url:
                return {"results": [_list_serializer_row(finding.source_id, item_key="SEC-9", status_category="done")]}
            return _detail_payload(finding.source_id, item_key="SEC-9", status_category="done")
        raise AssertionError(("unexpected write", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    args = type("Args", (), {"repo": "shizuha-labs/example", "ref": "main", "sha": "abc", "run_url": "http://run", "project_id": ""})()
    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)
    assert result == "skipped-terminal:SEC-9"
    # Only the GET lookup happened — no comment, no item create.
    assert all(c[0] == "GET" for c in calls)




def test_rejected_terminal_finding_is_skipped_even_without_status_category(monkeypatch):
    """Rejected security findings are terminal false-positive dispositions.

    Some legacy/list rows can expose `status=rejected` with no Status-table
    category. The deduper must still skip, not re-comment forever.
    """
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    calls = []

    def fake_request(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET":
            if "/items/?" in url:
                return {"results": [_list_serializer_row(
                    finding.source_id, item_key="SEC-FP", status="rejected", status_category=None,
                )]}
            return _detail_payload(finding.source_id, item_key="SEC-FP", status="rejected", status_category=None)
        raise AssertionError(("unexpected write", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    args = type("Args", (), {"repo": "shizuha-labs/example", "ref": "main", "sha": "abc", "run_url": "http://run", "project_id": ""})()

    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)

    assert result == "skipped-terminal:SEC-FP"
    assert all(c[0] == "GET" for c in calls)


def test_find_existing_uses_authoritative_source_id_not_fuzzy_search(monkeypatch):
    """PLAT-5442 AC2: dedupe must not depend on a fuzzy search= fallback.

    The 2026-08-15 live probe: source_id= alone matched the seven forked
    cortex ledgers, but ANDing search=<source_id> returned zero — ``search``
    does not index the ``source_id`` field and over-constrained every scan into
    minting a duplicate. The lookup must ship ONLY the exact key.
    """
    module = load_module()
    seen = []

    def fake_request(method, url, token, body=None):
        seen.append(url)
        return {"results": []}

    monkeypatch.setattr(module, "pulse_request", fake_request)
    module.pulse_find_existing("http://pulse/api", "tok", "security-ci:shizuha-labs/example:ledger")
    primary = seen[0]
    assert "source_id=" in primary
    assert "search=" not in primary


def test_find_existing_reconciles_forked_ledgers_to_oldest_non_terminal(monkeypatch):
    """PLAT-5442 AC1/AC5: forked ledgers update the OLDEST live copy in place.

    Live data 2026-08-15 had seven items for one cortex ledger source_id
    (rejected dupes + three live copies). The producer must reconcile to the
    oldest non-terminal (security closes the newer forks), never chase the
    newest item and never mint an eighth.
    """
    module = load_module()
    SID = "security-ci:shizuha-labs/cortex:ledger"
    ledger_rows = [
        {"id": 23478, "item_key": "PLAT-5426", "status": "rejected"},
        {"id": 22161, "item_key": "PLAT-5135", "status": "blocked"},  # oldest live
        {"id": 27104, "item_key": "PLAT-5998", "status": "blocked"},
    ]

    def fake_request(method, url, token, body=None):
        if method == "GET" and "/items/?" in url:
            return {"results": ledger_rows}
        if method == "GET":
            ident = int(url.rstrip("/").split("/")[-1])
            row = next(r for r in ledger_rows if r["id"] == ident)
            terminal = row["status"] == "rejected"
            return _detail_payload(
                SID,
                item_key=row["item_key"],
                status=row["status"],
                status_category="done" if terminal else "in_progress",
                ident=ident,
            )
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    got = module.pulse_find_existing("http://pulse/api", "tok", SID)
    assert got is not None
    assert got["item_key"] == "PLAT-5135"


def test_unconfirmed_list_row_fails_closed_never_patches_or_creates(monkeypatch):
    """PLAT-5442 AC3: a row whose source_id cannot be positively confirmed is
    never PATCHed and never papered over with a creation.

    Regression for the lint-surfaced latent path: if the list serializer omits
    source_id AND a detail fetch cannot confirm it, the old code would PATCH the
    first fuzzy row (clobbering an unrelated task). New behavior: fail closed.
    """
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    calls = []

    def fake_request(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET" and "/items/?" in url:
            return {"results": [_list_serializer_row(finding.source_id, item_key="SEC-X")]}
        if method == "GET":
            # Detail does NOT confirm: source_id is absent from the payload too.
            return {"id": "uuid-1", "item_key": "SEC-X", "status": "in_progress", "status_category": "in_progress"}
        raise AssertionError(("unexpected write", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    args = type("Args", (), {"repo": "shizuha-labs/example", "ref": "main", "sha": "abc", "run_url": "http://run", "project_id": ""})()
    try:
        module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)
        raise AssertionError("expected fail-closed RuntimeError")
    except RuntimeError as exc:
        assert "fail-closed" in str(exc)
    assert not any(c[0] in ("PATCH", "POST") for c in calls)


def test_ledger_absence_probe_fails_closed_when_filter_silently_broken(monkeypatch):
    """PLAT-5442 AC3: absence of the rolling ledger must be proven, not assumed.

    If the exact source_id= query returns zero but a ledger demonstrably exists
    (found via the ledger's title token + positive detail confirmation), the
    filter silently broke — creating a second ledger is exactly the fork this
    bug produced. Fail closed instead.
    """
    module = load_module()
    SID = "security-ci:shizuha-labs/deploy:ledger"
    calls = []

    def fake_request(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET" and "/items/?" in url and "search=" in url:
            # Bounded absence probe finds a ledger-shaped item.
            return {"results": [{"id": 999, "item_key": "PLAT-LED"}]}
        if method == "GET" and url.rstrip("/").endswith("/items/999"):
            return _detail_payload(SID, item_key="PLAT-LED", ident=999, status="blocked",
                                   status_category="in_progress")
        if method == "GET":
            return {"results": []}
        if method == "POST" and "/items" in url:
            calls.append(("POST/items",))
            return {"item_key": "PLAT-DUP"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    try:
        module.pulse_upsert_ledger("http://pulse/api", "tok", [], _ledger_args())
        raise AssertionError("expected fail-closed RuntimeError")
    except RuntimeError as exc:
        assert "fail-closed" in str(exc)
    assert not any(c == ("POST/items",) for c in calls)


def test_ledger_absent_path_still_creates_when_probe_finds_nothing(monkeypatch):
    """PLAT-5442: with the exact filter honored and genuinely nothing present,
    the probe confirms absence and the ledger create path proceeds (no fork)."""
    module = load_module()
    calls = []

    def fake_request(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET":
            return {"results": []}  # exact query empty AND probe empty
        if method == "POST" and url.rstrip("/").endswith("/items"):
            calls.append(("POST/items",))
            return {"id": "rollup-id", "item_key": "PLAT-NEW"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [], _ledger_args(),
        module.WikiLedgerResult(ok=True, page_url="https://wiki.shizuha.com/deploy-page", page_id="deploy-page", action="updated"),
    )
    assert result == "ledger-created:PLAT-NEW"
    assert ("POST/items",) in calls


def test_new_pulse_finding_payload_routes_security(monkeypatch):
    module = load_module()
    posts = []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            return {"results": []}
        posts.append(body)
        return {"item_key": "SEC-2"}

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_trivy(module.load_json(str(FIX / "trivy.json"))))
    args = type("Args", (), {
        "repo": "shizuha-labs/example", "ref": "main", "sha": "abcdef1",
        "run_url": "https://origin.shizuha.com/shizuha-labs/example/actions/runs/42",
        "project_id": "333",
    })()
    result = module.pulse_create_or_update_finding("http://pulse/api", "tok", finding, args)
    assert result == "created:SEC-2"
    body = posts[0]
    assert body["workflow_name"] == "security-finding"
    assert body["assignment_group"] == "security"
    assert body["source"] == "security-ci"
    assert body["source_id"].startswith("security-ci:")
    assert body["project"] == 333
    observation = body["metadata"]["origin_observations"][0]
    assert observation["provider"] == "origin-forgejo"
    assert observation["repo"] == "shizuha-labs/example"
    assert observation["workflow"] == "security-ci.yml"
    assert observation["check"] == f"security-ci:{finding.tool}"
    # Pulse's `/items/` `labels` is a flat list of strings (a dict is rejected
    # with HTTP 400 — the original security-ci filing failure), so scanner
    # metadata is encoded as `key:value` label strings.
    assert body["labels"] == [
        "security-ci",
        f"tool:{finding.tool}",
        "repo:shizuha-labs/example",
        f"severity:{finding.severity}",
    ]


def test_per_package_consolidation_collapses_cve_flood():
    """PLAT-2893: N advisories for one package collapse to ONE task listing all,
    not N tasks. Single-advisory packages and code SAST findings pass through."""
    module = load_module()
    F = module.Finding
    findings = [
        F(tool="osv", rule=f"PYSEC-2026-{i}", severity="high",
          title=f"django: PYSEC-2026-{i}", path="docs/requirements.txt", line=None,
          detail="d", package="django", ecosystem="PyPI")
        for i in range(19)
    ]
    # a second package with a SINGLE advisory (still package-keyed for idempotency) + code SAST
    findings.append(F(tool="osv", rule="GHSA-x", severity="high", title="idna: GHSA-x",
                      path="requirements.txt", line=None, detail="d",
                      package="idna", ecosystem="PyPI"))
    findings.append(F(tool="bandit", rule="B110", severity="high",
                      title="try/except/pass", path="tasks/x.py", line=42, detail="d"))

    out = module.consolidate_dependency_findings(findings, "shizuha-labs/shizuha-tasks")

    # 19 django -> 1 consolidated; idna (single) -> 1 package-keyed; bandit -> 1 unchanged.
    assert len(out) == 3
    deps = [f for f in out if f.tool == "deps"]
    assert len(deps) == 2
    dj = next(f for f in deps if f.package == "django")
    assert dj.package == "django"
    assert dj.title.startswith("Upgrade django")
    assert "19 advisories" in dj.detail
    assert "PYSEC-2026-0" in dj.detail and "PYSEC-2026-18" in dj.detail
    # single-advisory package is package-keyed from day one; code SAST is untouched.
    idna = next(f for f in deps if f.package == "idna")
    assert idna.rule == "pkg:shizuha-labs/shizuha-tasks:PyPI:idna"
    assert "1 advisories" in idna.detail
    assert any(f.tool == "bandit" and f.rule == "B110" for f in out)



def test_single_advisory_package_source_id_stable_when_second_advisory_appears():
    """PLAT-2893: a package must be package-keyed from its first advisory,
    so adding a later advisory upserts instead of forking a second Pulse task."""
    module = load_module()
    F = module.Finding
    one = [
        F(tool="osv", rule="CVE-1", severity="high", title="django: CVE-1",
          path="requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
    ]
    two = one + [
        F(tool="osv", rule="CVE-2", severity="high", title="django: CVE-2",
          path="requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
    ]

    a = module.consolidate_dependency_findings(one, "repo")[0]
    b = module.consolidate_dependency_findings(two, "repo")[0]

    assert a.tool == "deps" and b.tool == "deps"
    assert a.rule == "pkg:repo:PyPI:django"
    assert a.source_id == b.source_id


def test_package_source_id_stable_when_manifest_set_changes():
    """PLAT-2893: package identity must not depend on the lexicographically
    first manifest path present in the current advisory set."""
    module = load_module()
    F = module.Finding
    one = [
        F(tool="osv", rule="CVE-1", severity="high", title="django: CVE-1",
          path="z/requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
    ]
    two = one + [
        F(tool="osv", rule="CVE-2", severity="high", title="django: CVE-2",
          path="a/requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
    ]

    a = module.consolidate_dependency_findings(one, "repo")[0]
    b = module.consolidate_dependency_findings(two, "repo")[0]

    assert a.path == b.path == "deps/PyPI/django"
    assert a.source_id == b.source_id


def test_consolidated_source_id_stable_across_runs():
    """A newly-discovered advisory must UPSERT the same package task, not fork a
    new one — the consolidated source_id is independent of advisory count/severity."""
    module = load_module()
    F = module.Finding
    base = [
        F(tool="osv", rule=f"CVE-{i}", severity="high", title=f"django: CVE-{i}",
          path="requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
        for i in range(3)
    ]
    more = base + [
        F(tool="osv", rule="CVE-NEW", severity="critical", title="django: CVE-NEW",
          path="requirements.txt", line=None, detail="d", package="django", ecosystem="PyPI")
    ]
    a = module.consolidate_dependency_findings(base, "repo")[0]
    b = module.consolidate_dependency_findings(more, "repo")[0]
    assert a.tool == "deps" and b.tool == "deps"
    assert a.source_id == b.source_id


def test_missing_project_id_fails_closed_without_posting(monkeypatch):
    """PLAT-4164: URL+token may never permit an unscoped Pulse write."""
    module = load_module()
    posts = []

    def unexpected_post(*args, **kwargs):
        posts.append((args, kwargs))
        return "unexpected"

    monkeypatch.setattr(module, "pulse_create_or_update_finding", unexpected_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--bandit", str(FIX / "bandit.json"),
            "--pulse-api-url", "https://pulse.invalid/api",
            "--pulse-token", "fixture-token",
            "--project-id", "",
            "--fail-on", "critical",
        ],
    )

    assert module.main() == 2
    assert posts == []


def test_failed_pulse_ledger_delivery_returns_retryable_failure(monkeypatch, tmp_path):
    """PLAT-4772: losing the durable signal must fail into the bounded CI retry."""
    module = load_module()
    monkeypatch.setattr(
        module,
        "wiki_upsert_ledger",
        lambda *args, **kwargs: module.WikiLedgerResult(
            ok=False, error="GET Security space -> HTTP 404", action="write-failed"
        ),
    )
    monkeypatch.setattr(
        module,
        "pulse_upsert_ledger",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pulse unavailable")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--bandit", str(FIX / "bandit.json"),
            "--pulse-api-url", "https://pulse.invalid/api",
            "--pulse-token", "fixture-token",
            "--project-id", "333",
            "--fail-on", "critical",
            "--summary", str(tmp_path / "summary.md"),
        ],
    )

    assert module.main() == 2


def _ledger_args(**overrides):
    values = {
        "repo": "shizuha-labs/deploy",
        "ref": "master",
        "sha": "cd25ec1",
        "run_url": "https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/8670",
        "project_id": "333",
        "wiki_organization_id": 1,
        "wiki_index_page_id": "index-page-id",
    }
    values.update(overrides)
    return type("Args", (), values)()


def test_wiki_ledger_creates_missing_repo_page_before_pointer(monkeypatch):
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    calls = []

    def fake_wiki(method, url, token, body=None, *, organization_id=1):
        calls.append((method, url, body, organization_id))
        if url.endswith("/spaces/SEC/"):
            return {"id": "security-space"}
        if method == "GET" and "/pages/?" in url:
            return {"results": []}
        if method == "POST" and url.endswith("/pages/"):
            return {"id": "deploy-page", "title": "Findings Ledger — deploy"}
        raise AssertionError((method, url, body))

    monkeypatch.setattr(module, "wiki_request", fake_wiki)
    result = module.wiki_upsert_ledger("https://wiki.invalid/api", "tok", [finding], _ledger_args())

    assert result == module.WikiLedgerResult(
        ok=True,
        page_url="https://wiki.shizuha.com/deploy-page",
        page_id="deploy-page",
        action="created",
    )
    created = next(call[2] for call in calls if call[0] == "POST")
    assert created["space"] == "security-space"
    assert created["parent"] == "index-page-id"
    assert finding.source_id in created["content_text"]


def test_wiki_ledger_upsert_is_idempotent_when_projection_is_unchanged(monkeypatch):
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = _ledger_args()
    projection = module._replace_wiki_projection(
        "", module._wiki_projection([finding], args), args.repo
    )
    writes = []

    def fake_wiki(method, url, token, body=None, *, organization_id=1):
        if url.endswith("/spaces/SEC/"):
            return {"id": "security-space"}
        if method == "GET" and "/pages/?" in url:
            return {"results": [{"id": "deploy-page", "title": "Findings Ledger — deploy"}]}
        if method == "GET" and url.endswith("/pages/deploy-page/"):
            return {"id": "deploy-page", "version": 7, "content_text": projection}
        writes.append((method, url, body))
        return {}

    monkeypatch.setattr(module, "wiki_request", fake_wiki)
    result = module.wiki_upsert_ledger("https://wiki.invalid/api", "tok", [finding], args)

    assert result.ok is True
    assert result.action == "unchanged"
    assert writes == []


def test_wiki_projection_preserves_findings_beyond_legacy_200_row_cap():
    """PLAT-4772: the durable Wiki projection may not silently drop a finding."""
    module = load_module()
    findings = [
        module.Finding(
            tool="semgrep",
            rule=f"R{index}",
            severity="high",
            title=f"Finding {index}",
            path=f"src/file-{index}.py",
            line=index + 1,
            detail="fixture",
        )
        for index in range(201)
    ]

    projection = module._wiki_projection(findings, _ledger_args())

    assert "Active findings: **201**" in projection
    assert findings[-1].source_id in projection
    assert sum(finding.source_id in projection for finding in findings) == 201


def test_missing_space_or_failed_wiki_write_emits_bounded_pulse_telemetry(monkeypatch):
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = _ledger_args()

    def failed_wiki(method, url, token, body=None, *, organization_id=1):
        raise RuntimeError("GET Security space -> HTTP 404")

    monkeypatch.setattr(module, "wiki_request", failed_wiki)
    wiki_result = module.wiki_upsert_ledger("https://wiki.invalid/api", "tok", [finding], args)
    assert wiki_result.ok is False
    assert wiki_result.action == "write-failed"

    posts = []

    def fake_pulse(method, url, token, body=None):
        if method == "GET":
            return {"results": []}
        if method == "POST" and url.endswith("/items/"):
            posts.append(body)
            return {"id": "rollup-id", "item_key": "PLAT-PROBE"}
        raise AssertionError((method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_pulse)
    assert module.pulse_upsert_ledger(
        "https://pulse.invalid/api", "tok", [finding], args, wiki_result
    ) == "ledger-created:PLAT-PROBE"
    assert len(posts) == 1
    body = posts[0]
    assert body["assignment_group"] == "security"
    assert "durable-write-failed" in body["labels"]
    assert "No wiki pointer was emitted" in body["description"]
    assert "HTTP 404" in body["description"]
    assert "wiki.shizuha.com" not in body["description"]


def test_existing_page_failed_upsert_emits_no_dead_pointer(monkeypatch):
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = _ledger_args()

    def failed_update(method, url, token, body=None, *, organization_id=1):
        if url.endswith("/spaces/SEC/"):
            return {"id": "security-space"}
        if method == "GET" and "/pages/?" in url:
            return {"results": [{"id": "deploy-page", "title": "Findings Ledger — deploy"}]}
        if method == "GET" and url.endswith("/pages/deploy-page/"):
            return {"id": "deploy-page", "version": 7, "content_text": "# Existing notes"}
        if method == "PATCH" and url.endswith("/pages/deploy-page/"):
            assert body["expected_version"] == 7
            raise RuntimeError("PATCH Wiki page -> HTTP 409: version_conflict")
        raise AssertionError((method, url, body))

    monkeypatch.setattr(module, "wiki_request", failed_update)
    wiki_result = module.wiki_upsert_ledger(
        "https://wiki.invalid/api", "tok", [finding], args
    )
    assert wiki_result.ok is False
    assert wiki_result.action == "write-failed"
    assert "version_conflict" in wiki_result.error

    posts = []

    def fake_pulse(method, url, token, body=None):
        if method == "GET":
            return {"results": []}
        if method == "POST" and url.endswith("/items/"):
            posts.append(body)
            return {"id": "rollup-id", "item_key": "PLAT-PROBE"}
        raise AssertionError((method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_pulse)
    module.pulse_upsert_ledger(
        "https://pulse.invalid/api", "tok", [finding], args, wiki_result
    )
    assert "durable-write-failed" in posts[0]["labels"]
    assert "wiki.shizuha.com" not in posts[0]["description"]


def test_successful_wiki_upsert_is_the_only_pointer_in_rollup(monkeypatch):
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = _ledger_args()
    posts = []

    def fake_pulse(method, url, token, body=None):
        if method == "GET":
            return {"results": []}
        posts.append(body)
        return {"id": "rollup-id", "item_key": "PLAT-PROBE"}

    monkeypatch.setattr(module, "pulse_request", fake_pulse)
    result = module.WikiLedgerResult(
        ok=True,
        page_url="https://wiki.shizuha.com/deploy-page",
        page_id="deploy-page",
        action="updated",
    )
    module.pulse_upsert_ledger("https://pulse.invalid/api", "tok", [finding], args, result)

    body = posts[0]
    assert "[Findings Ledger — deploy](https://wiki.shizuha.com/deploy-page)" in body["description"]
    assert "durable-write-failed" not in body["labels"]
    assert "wiki → Security" not in body["description"]


def _valid_allow_entry(**overrides):
    base = {
        "tool": "bandit",
        "rule": "B602",
        "path_contains": "app.py",
        "disposition": "accepted-false-positive",
        "reason": "fixture false positive",
        "owner": "security@shizuha.com",
        "approved_by": "security@shizuha.com",
        "expires": "2099-12-31",
    }
    base.update(overrides)
    return base


def test_malformed_allowlist_entries_fail_closed():
    """PLAT-4772 P1: {}, selector-less, expired, non-Security rows never suppress."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    now = module.datetime(2090, 1, 1, tzinfo=module.timezone.utc)

    cases = [
        {},
        {"reason": "only reason", "owner": "security", "disposition": "accepted-false-positive",
         "approved_by": "security", "expires": "2099-12-31"},
        {"tool": "bandit", "reason": "tool only", "owner": "security",
         "disposition": "accepted-false-positive", "approved_by": "security",
         "expires": "2099-12-31"},
        {"path_contains": "app.py", "reason": "path only", "owner": "security",
         "disposition": "accepted-false-positive", "approved_by": "security",
         "expires": "2099-12-31"},
        # Bare tool+rule is class-wide — rejected even with Security + suppressing disposition.
        {"tool": "bandit", "rule": "B602", "reason": "class-wide bare tool+rule",
         "owner": "security", "disposition": "accepted-false-positive",
         "approved_by": "security", "expires": "2099-12-31"},
        _valid_allow_entry(expires="2000-01-01"),
        _valid_allow_entry(owner="engineering", approved_by="engineering-lead"),
        _valid_allow_entry(disposition=""),
        _valid_allow_entry(reason=""),
        "not-an-object",
    ]
    for entry in cases:
        rejects: list[str] = []
        assert module.allowlisted(finding, [entry], now=now, reject_sink=rejects) is None
        assert rejects, f"expected rejection for {entry!r}"

    # Control: a fully valid Security-approved path-scoped entry still suppresses.
    ok = module.allowlisted(finding, [_valid_allow_entry()], now=now)
    assert ok is not None
    assert "fixture false positive" in ok
    assert "accepted-false-positive" in ok

    # Control: source_id-keyed entry still suppresses without path_contains.
    sid_ok = module.allowlisted(
        finding,
        [_valid_allow_entry(
            source_id=finding.source_id,
            tool="",
            rule="",
            path_contains="",
        )],
        now=now,
    )
    assert sid_ok is not None


def test_allowlist_rejects_rei_p1_class_wide_spoof_and_deferred():
    """PLAT-4772 rei P1: bare tool+rule + not-security + deferred must not suppress.

    Reproduction from Request Changes @ d261e50e:
      entry = {tool:bandit, rule:B104, disposition:deferred, approved_by:not-security, ...}
      validate_allowlist_entry(entry) must reject; allowlisted() must not suppress.
    """
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    now = module.datetime(2090, 1, 1, tzinfo=module.timezone.utc)

    # Exact rei reproduction (class-wide B104, deferred, spoofed approver).
    rei_entry = {
        "tool": "bandit",
        "rule": "B104",
        "disposition": "deferred",
        "reason": "parked",
        "owner": "engineering",
        "approved_by": "not-security",
        "expires": "2099-12-31",
    }
    reason = module.validate_allowlist_entry(rei_entry, now=now)
    assert reason is not None
    rejects: list[str] = []
    assert module.allowlisted(finding, [rei_entry], now=now, reject_sink=rejects) is None
    assert rejects

    # Spoofed Security substring alone (otherwise fully path-scoped + suppressing).
    spoof = _valid_allow_entry(owner="engineering", approved_by="not-security")
    assert module.validate_allowlist_entry(spoof, now=now) is not None
    assert module._is_security_approver("not-security") is False
    assert module._is_security_approver("unsecurity@shizuha.com") is False
    assert module._is_security_approver("security@shizuha.com") is True
    assert module._is_security_approver("security-lead") is True

    # deferred is a ledger state — never a suppressing disposition.
    deferred = _valid_allow_entry(disposition="deferred")
    deferred_reason = module.validate_allowlist_entry(deferred, now=now)
    assert deferred_reason is not None
    assert "deferred" in deferred_reason
    assert module.allowlisted(finding, [deferred], now=now, reject_sink=[]) is None

    # open likewise never suppresses.
    open_entry = _valid_allow_entry(disposition="open")
    assert module.validate_allowlist_entry(open_entry, now=now) is not None

    # Bare tool+rule with legitimate Security + accepted-false-positive still rejected.
    bare = {
        "tool": "bandit",
        "rule": finding.rule,
        "disposition": "accepted-false-positive",
        "reason": "would be class-wide",
        "owner": "security@shizuha.com",
        "approved_by": "security@shizuha.com",
        "expires": "2099-12-31",
    }
    bare_reason = module.validate_allowlist_entry(bare, now=now)
    assert bare_reason is not None
    assert "class-wide" in bare_reason or "exact identity" in bare_reason
    assert module.allowlisted(finding, [bare], now=now, reject_sink=[]) is None


def test_allowlist_expired_and_unauthorized_do_not_match_via_cli(tmp_path):
    """End-to-end: expired / non-Security allowlist rows leave findings active."""
    expired = tmp_path / "expired.json"
    expired.write_text(json.dumps({"allowlist": [
        _valid_allow_entry(expires="2000-01-01"),
        _valid_allow_entry(owner="engineering", approved_by="eng-lead",
                           reason="not security approved"),
        _valid_allow_entry(tool="bandit", rule="B602", path_contains="app.py",
                           reason="", disposition="accepted-false-positive"),
    ]}))
    proc = run_script(
        "--bandit", str(FIX / "bandit.json"),
        "--dry-run",
        "--fail-on", "critical",
        "--allowlist", str(expired),
        "--summary", str(tmp_path / "summary.md"),
    )
    assert proc.returncode == 0
    assert "1 active finding(s), 0 suppressed" in proc.stdout
    assert "allowlist entry" in proc.stderr
    assert "rejected" in proc.stderr


def test_existing_ledger_patch_failure_with_wiki_failure_propagates(monkeypatch):
    """PLAT-4772 P1: Wiki fail + existing-item PATCH fail must not exit quietly.

    Exercises the real ``pulse_upsert_ledger`` existing-item path (not a wholesale
    mock of the function). Expects: durable-write-failed comment content AND a
    raised error so main() can return 2.
    """
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    args = _ledger_args()
    wiki_result = module.WikiLedgerResult(
        ok=False,
        error="Wiki HTTP 404",
        action="write-failed",
    )
    comments: list[str] = []
    patches = {"n": 0}

    def fake_pulse(method, url, token, body=None):
        if method == "GET" and "/items/?" in url:
            # List serializer shape: lightweight row (no source_id/description).
            return {"results": [{
                "id": "rollup-id",
                "item_key": "SEC-1",
                "labels": ["security-ci", "security-ci:ledger", "ledger-hash:olddeadbeef"],
            }]}
        if method == "GET":
            # Detail fetch: positively confirms source_id (PLAT-5442 guard).
            return {
                "id": "rollup-id",
                "item_key": "SEC-1",
                "source_id": "security-ci:shizuha-labs/deploy:ledger",
                "status": "in_progress",
                "status_category": "in_progress",
                "labels": ["security-ci", "security-ci:ledger", "ledger-hash:olddeadbeef"],
            }
        if method == "PATCH" and "/items/" in url and not url.rstrip("/").endswith("comments"):
            patches["n"] += 1
            raise RuntimeError("PATCH Pulse item -> HTTP 500: boom")
        if method == "POST" and url.rstrip("/").endswith("comments"):
            comments.append((body or {}).get("content") or (body or {}).get("body") or "")
            return {"id": "c1"}
        # pulse_comment may POST to /items/{id}/comments/
        if method == "POST" and "comment" in url:
            comments.append(str(body))
            return {"id": "c1"}
        raise AssertionError((method, url, body))

    def fake_comment(api_base, token, ref, content):
        comments.append(content)
        return None

    monkeypatch.setattr(module, "pulse_request", fake_pulse)
    monkeypatch.setattr(module, "pulse_comment", fake_comment)

    with __import__("pytest").raises(RuntimeError) as excinfo:
        module.pulse_upsert_ledger(
            "https://pulse.invalid/api", "tok", [finding], args, wiki_result
        )
    assert patches["n"] == 1
    assert "Wiki ledger failed" in str(excinfo.value)
    assert "PATCH failed" in str(excinfo.value)
    assert comments, "expected a durable-write-failed fallback comment"
    assert any("durable-write-failed" in c for c in comments)
    assert any("Wiki HTTP 404" in c or "write-failed" in c for c in comments)


def test_main_returns_2_when_wiki_and_pulse_ledger_both_fail(monkeypatch, tmp_path):
    """main() must exit 2 when pulse_upsert_ledger raises after Wiki failure."""
    module = load_module()
    bandit = FIX / "bandit.json"

    monkeypatch.setattr(module, "wiki_upsert_ledger", lambda *a, **k: module.WikiLedgerResult(
        ok=False, error="Wiki HTTP 404", action="write-failed",
    ))

    def boom(*a, **k):
        raise RuntimeError("Wiki ledger failed (write-failed: Wiki HTTP 404); Pulse PATCH failed for SEC-1: boom")

    monkeypatch.setattr(module, "pulse_upsert_ledger", boom)

    argv = [
        "security-ci-to-pulse.py",
        "--bandit", str(bandit),
        "--repo", "shizuha-labs/deploy",
        "--fail-on", "critical",
        "--pulse-url", "https://pulse.invalid",
        "--pulse-token", "tok",
        "--project-id", "1",
        "--wiki-api-url", "https://wiki.invalid/api",
        "--wiki-token", "wtok",
        "--allowlist", str(tmp_path / "empty.json"),
    ]
    (tmp_path / "empty.json").write_text('{"allowlist":[]}')
    monkeypatch.setattr(module.sys, "argv", argv)
    # Ensure env min severity doesn't filter the finding away from ledger path
    monkeypatch.setenv("SECURITY_CI_MIN_FILE_SEVERITY", "low")
    rc = module.main()
    assert rc == 2


# --- PLAT-8903: one stable per-repo rollup + branch-posture isolation -------


def test_ledger_ambiguity_fails_loud_without_writing(monkeypatch):
    """Two live rollups sharing the ledger source_id must fail LOUD with no
    posture write (PLAT-8903: the oldest-first election upserted the stale
    generation PLAT-4699 over the wiki-current PLAT-6986 and a fix-branch scan
    then wrote a stale 12-row posture into it). The producer cannot safely
    elect between generations — created_at ordering picked the WRONG one and
    open-status ordering would too (the stale generation is the open one) —
    so any mixed/live multi-match set is a loud reconciliation failure."""
    module = load_module()
    requests = []
    # nagi re-anchor (PLAT-10007 composition): created_at added to match the
    # documented real incident shape — the stale ACTIVE generation is OLDER
    # than the completed current one, which is precisely the zombie-active
    # case the composed resolver fails loud on.
    stale = _list_serializer_row(
        "security-ci:shizuha-labs/deploy:ledger", item_key="PLAT-4699",
        status="in_verification", status_category="in_progress",
    )
    stale["id"] = "uuid-stale"
    stale["created_at"] = "2026-07-16T00:00:00Z"
    current = _list_serializer_row(
        "security-ci:shizuha-labs/deploy:ledger", item_key="PLAT-6986",
        status="completed", status_category="done",
    )
    current["id"] = "uuid-current"
    current["created_at"] = "2026-09-10T00:00:00Z"

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        if method == "GET" and "/items/?" in url:
            # List serializer omits source_id (PLAT-2688); the reconciled
            # PLAT-5442 contract then positively confirms each candidate
            # with a detail fetch before anything can be elected/written.
            return {"results": [stale, current]}
        if method == "GET":
            ident = url.rstrip("/").split("/")[-1]
            row = stale if ident == "uuid-stale" else current
            return _detail_payload(
                "security-ci:shizuha-labs/deploy:ledger",
                item_key=row["item_key"],
                status=row["status"],
                status_category=row["status_category"],
                created_at=row["created_at"],
                ident=ident,
            )
        raise AssertionError(("no write may occur on ambiguity", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    with pytest.raises(RuntimeError) as excinfo:
        module.pulse_upsert_ledger("http://pulse/api", "tok", [finding], _ledger_args())
    # The failure names both generations so reconciliation is first-attempt actionable.
    assert "PLAT-4699" in str(excinfo.value)
    assert "PLAT-6986" in str(excinfo.value)
    assert "NO posture was written" in str(excinfo.value)
    # And no write happened: only the lookup GETs.
    assert not any(req[0] in ("PATCH", "POST") for req in requests)


def test_ledger_branch_scan_does_not_write_stable_rollup(monkeypatch):
    """A fix-branch scan is behind main — its posture must NOT be advertised
    into the stable per-repo rollup (PLAT-8903: run 5830 on
    fix/security-ci-origin-checkout wrote a stale 12-row set into the drive
    rollup). Branch telemetry stays branch-scoped."""
    module = load_module()
    requests = []

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        raise AssertionError(("no Pulse call may occur for a branch scan", method, url))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [finding],
        _ledger_args(ref="fix/security-ci-origin-checkout"),
    )
    assert result == "ledger-skipped-branch:fix/security-ci-origin-checkout"
    assert requests == [], "branch scans must not touch the stable rollup at all"


def test_ledger_scheduled_run_fully_qualified_ref_updates_stable_rollup(monkeypatch):
    """Scheduled/push runs provide GITHUB_REF fully qualified
    ("refs/heads/master") and the workflow does not pass --ref, so the
    branch-posture gate must compare the SHORT branch name. Regression for
    the PLAT-8903 follow-up: the unqualified comparison classified every
    scheduled run as a branch scan and silently skipped the stable rollup
    forever (ledger stale, no error surfaced)."""
    module = load_module()
    requests = []
    # nagi re-anchor (PLAT-10007 composition): status is a live rollup — this
    # test's subject is ref normalization (the PLAT-8903 follow-up), and
    # terminal-only sets are now skip+refuse per PLAT-10007.
    rows = [_list_serializer_row(
        "security-ci:shizuha-labs/deploy:ledger", item_key="PLAT-6986",
        status="in_progress", status_category="in_progress",
    )]

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        return _confirmed_lookup_fake(
            "security-ci:shizuha-labs/deploy:ledger",
            rows,
            handlers=lambda m, u, b: {"item_key": "PLAT-6986"} if m == "PATCH" else None,
        )(method, url, token, body)

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [finding],
        _ledger_args(ref="refs/heads/master"),
    )
    assert result.startswith("ledger-updated:PLAT-6986") or result.startswith("ledger-unchanged:PLAT-6986")
    assert any(req[0] == "PATCH" for req in requests), "scheduled run must update the stable rollup in place"


def test_ledger_fully_qualified_branch_ref_still_skipped(monkeypatch):
    """A fully qualified NON-default ref ("refs/heads/fix/x") is still a
    branch scan: prefix normalization must not wave it through."""
    module = load_module()
    requests = []

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        raise AssertionError(("no Pulse call may occur for a branch scan", method, url))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [finding],
        _ledger_args(ref="refs/heads/fix/security-ci-origin-checkout"),
    )
    assert result == "ledger-skipped-branch:refs/heads/fix/security-ci-origin-checkout"
    assert requests == []


def test_ledger_empty_ref_fails_closed_to_branch_skip(monkeypatch):
    """An empty/unknown ref must never write the stable rollup (fail closed):
    only a recognized default-branch ref may update it."""
    module = load_module()
    requests = []

    def fake_request(method, url, token, body=None):
        requests.append((method, url, body))
        raise AssertionError(("no Pulse call may occur for an unknown ref", method, url))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    result = module.pulse_upsert_ledger(
        "http://pulse/api", "tok", [finding], _ledger_args(ref=""),
    )
    assert result.startswith("ledger-skipped-branch")
    assert requests == []


def _confirmed_lookup_fake(source_id, rows, handlers=None):
    """A fake_request modeling the reconciled lookup contract (PLAT-5442).

    Serves ``rows`` from the list endpoint (serializer omits ``source_id``)
    and positively confirms each candidate from a per-id detail payload —
    exactly what the production confirmation layer does. ``handlers``
    receives ``(method, url, body)`` after the lookup GETs and returns a
    payload or None (None -> unexpected-call AssertionError).
    """
    def fake_request(method, url, token, body=None):
        if method == "GET" and "/items/?" in url:
            return {"results": list(rows)}
        if method == "GET":
            ident = url.rstrip("/").split("/")[-1]
            row = next(r for r in rows if str(r.get("id")) == ident)
            detail = _detail_payload(
                source_id,
                item_key=row.get("item_key", "SEC-1"),
                status=row.get("status", "in_progress"),
                status_category=row.get("status_category", "in_progress"),
                severity=row.get("severity"),
                labels=row.get("labels"),
                created_at=row.get("created_at"),
                ident=str(row.get("id")),
            )
            return detail
        if handlers:
            served = handlers(method, url, body)
            if served is not None:
                return served
        raise AssertionError(("unexpected", method, url, body))
    return fake_request


# PLAT-10007 SUPERSESSION (nagi re-anchor, sara's reviewed contract): a lone
# TERMINAL generation is never a write target — the scan skips and refuses to
# re-mint with the fail-loud attribution log (Security owns the next
# generation). Master's in-place-update-for-terminal behavior is retired;
# covered now by test_stale_gen_replay_onto_terminal_ledger_is_skipped_loudly.
# PLAT-10007 SUPERSESSION (nagi re-anchor, confirmed composition with sara):
# master's test_ledger_all_archived_history_updates_most_recent_generation is
# RETIRED — an all-done match set is terminal generations (completed/rejected
# dispositions), and the scan producer must NOT replay into any of them.
# Security owns the next generation; the upsert skips and refuses to re-mint
# with the fail-loud attribution log (test_stale_gen_replay_onto_terminal_
# ledger_is_skipped_loudly covers the behavior). The PLAT-8903 fail-loud on
# multiple ACTIVE generations is preserved inside pulse_resolve_ledger_target.

# ---------------------------------------------------------------------------
# PLAT-8988: requirements.txt unreachability guard
# ---------------------------------------------------------------------------

def _osv_json_for(pkg: str, version: str, advisories: list[dict]) -> dict:
    """Minimal osv-scanner JSON: one requirements.txt source, one package, N
    advisories with SEMVER fixed events."""
    return {
        "results": [
            {
                "source": {"path": f"/workspace/shizuha-labs/example/plat8988-fixture/requirements.txt", "type": "requirement"},
                "packages": [
                    {
                        "package": {"name": pkg, "ecosystem": "PyPI", "version": version},
                        "vulnerabilities": advisories,
                    }
                ],
            }
        ]
    }


def _adv(vuln_id: str, fixed: str) -> dict:
    return {
        "id": vuln_id,
        "summary": f"{vuln_id} test advisory",
        "affected": [
            {
                "package": {"name": "python-multipart", "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": fixed}]}],
            }
        ],
    }


def test_plat8988_guard_drops_floor_above_every_fix(tmp_path, monkeypatch):
    """Regression fixture for the real PLAT-7086 emission: python-multipart
    0.0.32 with requirement floor >=0.0.32 vs 5 advisories whose highest fix is
    0.0.30 — the sweep must emit ZERO rows."""
    module = load_module()
    advisories = [
        _adv("GHSA-2jv5-9r88-3w3p", "0.0.7"),
        _adv("GHSA-59g5-xgcq-4qw3", "0.0.18"),
        _adv("GHSA-wp53-j4wj-2cfg", "0.0.22"),
        _adv("GHSA-pp6c-gr5w-3c5g", "0.0.27"),
        _adv("GHSA-5rvq-cxj2-64vf", "0.0.30"),
    ]
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("python-multipart>=0.0.32,<0.0.33\n")
    data = _osv_json_for("python-multipart", "0.0.32", advisories)
    findings = list(module.parse_osv(data))
    assert len(findings) == 5
    for f in findings:
        object.__setattr__(f, "path", str(manifest))
    kept, dropped = module.drop_unreachable_requirement_findings(list(findings))
    assert kept == []
    assert len(dropped) == 5
    assert all("0.0.32" in reason for _f, reason in dropped)


def test_plat8988_guard_keeps_floor_below_a_fix(tmp_path):
    """A floor below any fix threshold means the advisory is reachable — keep."""
    module = load_module()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("python-multipart>=0.0.20\n")
    data = _osv_json_for("python-multipart", "0.0.20", [_adv("GHSA-5rvq-cxj2-64vf", "0.0.30")])
    findings = [f._replace(path=str(manifest)) if hasattr(f, "_replace") else f for f in module.parse_osv(data)]
    findings = [
        module.Finding(**{**f.__dict__, "path": str(manifest)}) for f in findings
    ]
    kept, dropped = module.drop_unreachable_requirement_findings(findings)
    assert len(kept) == 1 and not dropped


def test_plat8988_guard_fails_open_on_unbounded_requirement(tmp_path):
    module = load_module()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("python-multipart\n")
    data = _osv_json_for("python-multipart", "0.0.20", [_adv("GHSA-5rvq-cxj2-64vf", "0.0.30")])
    findings = [module.Finding(**{**f.__dict__, "path": str(manifest)}) for f in module.parse_osv(data)]
    kept, dropped = module.drop_unreachable_requirement_findings(findings)
    assert len(kept) == 1 and not dropped


def test_plat8988_guard_fails_open_on_missing_fixed_event(tmp_path):
    """An advisory with a last_affected (unbounded) range is never dropped."""
    module = load_module()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("python-multipart>=9.9.9\n")
    vuln = {
        "id": "GHSA-unbounded",
        "affected": [
            {
                "package": {"name": "python-multipart", "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"last_affected": "0.0.10"}]}],
            }
        ],
    }
    data = _osv_json_for("python-multipart", "9.9.9", [vuln])
    findings = [module.Finding(**{**f.__dict__, "path": str(manifest)}) for f in module.parse_osv(data)]
    assert all(f.fixed_versions == () for f in findings)
    kept, dropped = module.drop_unreachable_requirement_findings(findings)
    assert len(kept) == 1 and not dropped


def test_plat8988_guard_fails_open_on_unreadable_manifest(tmp_path):
    module = load_module()
    data = _osv_json_for("python-multipart", "0.0.32", [_adv("GHSA-5rvq-cxj2-64vf", "0.0.30")])
    findings = list(module.parse_osv(data))  # path does not exist on disk
    kept, dropped = module.drop_unreachable_requirement_findings(findings)
    assert len(kept) == 1 and not dropped


def test_plat8988_guard_never_touches_locked_manifests_or_other_tools(tmp_path):
    """uv.lock / Cargo.lock resolve exact versions — the guard must not apply.
    trivy findings are out of scope too."""
    module = load_module()
    lock = tmp_path / "uv.lock"
    lock.write_text('name = "python-multipart"\nversion = "0.0.20"\n')
    f_osv_lock = module.Finding(
        tool="osv", rule="GHSA-5rvq-cxj2-64vf", severity="high",
        title="python-multipart: GHSA-5rvq-cxj2-64vf", path=str(lock), line=None,
        detail="x", package="python-multipart", ecosystem="PyPI",
        fixed_versions=("0.0.30",),
    )
    f_trivy = module.Finding(
        tool="trivy", rule="CVE-2026-0001", severity="high",
        title="python-multipart: CVE-2026-0001", path=str(lock), line=None,
        detail="x", package="python-multipart", ecosystem="PyPI",
        fixed_versions=("0.0.30",),
    )
    kept, dropped = module.drop_unreachable_requirement_findings([f_osv_lock, f_trivy])
    assert len(kept) == 2 and not dropped


def test_plat8988_guard_end_to_end_cli_drops_before_consolidation(tmp_path):
    """CLI-level: the dropped advisories never reach the consolidated deps
    identity — the run reports zero active findings and exits 0."""
    manifest = tmp_path / "plat8988-fixture" / "requirements.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("python-multipart>=0.0.32\n")
    data = _osv_json_for("python-multipart", "0.0.32", [
        _adv("GHSA-2jv5-9r88-3w3p", "0.0.7"),
        _adv("GHSA-5rvq-cxj2-64vf", "0.0.30"),
    ])
    osv_file = tmp_path / "osv.json"
    osv_file.write_text(json.dumps(data))
    # The manifest path inside the JSON points at /workspace/... which does not
    # exist here — the guard's relative-path fallback maps it under CWD. Run
    # from tmp_path so the fallback resolves to the fixture manifest.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--osv", str(osv_file), "--repo",
         "shizuha-labs/example", "--dry-run", "--fail-on", "high",
         "--summary", str(tmp_path / "summary.md")],
        text=True, capture_output=True, cwd=tmp_path,
    )
    out = proc.stdout + proc.stderr
    assert "PLAT-8988 guard dropped 2" in out
    assert "security-ci: 0 active finding(s)" in out
    assert proc.returncode == 0

# --- PLAT-10007: stale-generation ledger replay guard -----------------------


def _ledger_row(item_key, *, status, status_category, created_at):
    """List-serializer ledger row with explicit generation ordering.

    nagi re-anchor (post-#423): rows carry ``source_id`` so
    ``pulse_find_existing_all``'s PLAT-5442 positive confirmation succeeds
    from the row itself (the ledger upsert path consumes confirmed rows)."""
    return {
        "id": f"uuid-{item_key}", "item_key": item_key,
        "source_id": "security-ci:shizuha-labs/deploy:ledger",
        "status": status, "status_category": status_category,
        "created_at": created_at,
    }


def test_stale_gen_replay_onto_terminal_ledger_is_skipped_loudly(monkeypatch, capsys):
    """AC regression (PLAT-10007): a July-head replay against a repo whose
    only ledger generations are terminal/superseded is skipped with the
    fail-loud actor-attribution log — no PATCH, no re-mint.

    Live shape (2026-09-26): PLAT-5745 (rejected, superseded generation) +
    PLAT-6278 (terminal canonical); run-18970 replayed six remediated rows
    onto PLAT-5745."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    writes = []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            return {"results": [
                _ledger_row("PLAT-5745", status="rejected", status_category="done",
                            created_at="2026-07-01T00:00:00Z"),
                _ledger_row("PLAT-6278", status="completed", status_category="done",
                            created_at="2026-08-01T00:00:00Z"),
            ]}
        if method in ("PATCH", "POST"):
            writes.append((method, url, body))
            return {"item_key": "SHOULD-NOT-EXIST"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    result = module.pulse_upsert_ledger("http://pulse/api", "tok", [finding], _ledger_args())
    assert result.startswith("ledger-skipped-stale-gen:PLAT-5745")
    assert "PLAT-6278" in result
    assert writes == []  # no upsert, no re-mint
    err = capsys.readouterr().err
    assert "SKIP stale-gen ledger replay" in err
    assert "PLAT-5745(rejected)" in err and "PLAT-6278(completed)" in err
    # Actor attribution: producer run + sha named.
    assert "run https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/8670" in err
    assert "cd25ec1" in err


def test_active_ledger_generation_wins_over_stale_carrier(monkeypatch, capsys):
    """When an active generation coexists with a rejected stale carrier, the
    upsert targets the ACTIVE generation (most recent non-terminal) and logs
    the skipped stale carrier — the old oldest-first targeting would have
    written into the rejected item."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    patches = []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            return {"results": [
                _ledger_row("PLAT-5745", status="rejected", status_category="done",
                            created_at="2026-07-01T00:00:00Z"),
                _ledger_row("PLAT-6986", status="open", status_category="in_progress",
                            created_at="2026-08-01T00:00:00Z"),
            ]}
        if method == "PATCH":
            patches.append((url, body))
            return {"item_key": "PLAT-6986"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    result = module.pulse_upsert_ledger("http://pulse/api", "tok", [finding], _ledger_args())
    assert result == "ledger-updated:PLAT-6986"
    assert len(patches) == 1 and "PLAT-6986" in patches[0][0]
    err = capsys.readouterr().err
    assert "PLAT-5745(rejected)" in err and "PLAT-6986" in err


def test_active_ready_ledger_path_unchanged_by_generation_guard(monkeypatch, capsys):
    """AC 2: a single active/ready ledger behaves exactly as before — in-place
    PATCH, no skip log, no generation noise."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    patches = []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            return {"results": [
                _ledger_row("PLAT-5840", status="open", status_category="in_progress",
                            created_at="2026-09-01T00:00:00Z"),
            ]}
        if method == "PATCH":
            patches.append(url)
            return {"item_key": "PLAT-5840"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    result = module.pulse_upsert_ledger("http://pulse/api", "tok", [finding], _ledger_args())
    assert result == "ledger-updated:PLAT-5840"
    assert len(patches) == 1
    err = capsys.readouterr().err
    assert "SKIP stale-gen" not in err


def test_fresh_findings_onto_all_archived_repo_still_skip_and_refuse(monkeypatch, capsys):
    """PLAT-10007 edge (sara amendment): a scan with genuinely NEW findings
    against a repo whose only ledger generations are all terminal/completed
    takes the same skip+loud-log path — the producer never silently
    resurrects a closed generation; Security mints/reopens per PLAT-4772."""
    module = load_module()
    finding = next(module.parse_bandit(module.load_json(str(FIX / "bandit.json"))))
    writes = []

    def fake_request(method, url, token, body=None):
        if method == "GET":
            return {"results": [
                _ledger_row("PLAT-OLD", status="completed", status_category="done",
                            created_at="2026-06-01T00:00:00Z"),
                _ledger_row("PLAT-NEWER", status="completed", status_category="done",
                            created_at="2026-08-01T00:00:00Z"),
            ]}
        if method in ("PATCH", "POST"):
            writes.append((method, url, body))
            return {"item_key": "SHOULD-NOT-EXIST"}
        raise AssertionError(("unexpected", method, url, body))

    monkeypatch.setattr(module, "pulse_request", fake_request)
    result = module.pulse_upsert_ledger("http://pulse/api", "tok", [finding], _ledger_args())
    assert result.startswith("ledger-skipped-stale-gen:PLAT-NEWER")
    assert "PLAT-OLD" in result
    assert writes == []
    err = capsys.readouterr().err
    assert "SKIP stale-gen ledger replay" in err
    assert "PLAT-OLD(completed)" in err and "PLAT-NEWER(completed)" in err
