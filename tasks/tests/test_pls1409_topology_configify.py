"""PLS-1409 / CON-466: env-only topology configify tests.

Two properties are proven:
1. DEFAULTS: a process configured with the first-party anchor env keeps
   first-party behaviour identical — every URL/regex built by
   tasks.origin_provenance and the company-state source defaults still name
   the first-party endpoints (live Pulse does not break).
2. ENV-ONLY OVERRIDE: with the topology env set BEFORE process start, no
   constructed secondary URL (ci-logs, books, alertmanager, prometheus) and
   no provenance canonical form assumes ``origin.shizuha.com`` /
   ``*.svc.cluster.local`` — a third-party clone runs fully on its own
   endpoints, and first-party hosts fail closed (they are no longer the
   deployment's trust anchors).

orig18-v3 constraint (source-authority contract, PLAT-9778 family): the
provenance parser's trust anchor must live in `tasks/origin_provenance.py`
and be validated HTTPS-only/no-credentials/no-path with a fail-loud
ValueError at import time. The compliant env form is the deployment-owned
``ORIGIN_PUBLIC_URL`` anchor (+ ``ORIGIN_*_HOST_ALIASES``), which these
probes use; the PULSE_ORIGIN_* vars drive the secondary construction
surfaces only.

The override cases run in a subprocess (topology is read at import time, the
same contract as Django settings). Probe URLs are built from the deployment's
own topology values — a first-party URL literal would be meaningless input in
a third-party process.

PLAT/discovery note: these live under ``tasks/tests/`` so they are gated by
the premerge unittest leg (``manage.py test tasks``), which collects only
``TestCase`` subclasses — module-level pytest functions here would gate
nothing (the pytest leg covers the root ``tests/`` directory only).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]

FIRST_PARTY = {
    "public": "https://origin.shizuha.com",
    "internal_http": "http://forgejo-http.origin.svc.cluster.local",
    "ci_logs": "http://ci-logs.origin.svc.cluster.local:8080",
}

# The first-party deployment env: the provenance parser's ORIGIN_PUBLIC_URL
# anchor + internal aliases (what production sets), so parser construction
# names first-party endpoints exactly as live Pulse does.
FIRST_PARTY_ENV = {
    "ORIGIN_PUBLIC_URL": "https://origin.shizuha.com",
    "ORIGIN_INTERNAL_HOST_ALIASES": (
        "forgejo-http.origin.svc,forgejo-http.origin.svc.cluster.local"
    ),
}

THIRD_PARTY_ENV = {
    # Provenance trust anchor: the orig18-v3-compliant env form, validated
    # HTTPS-only at import inside tasks/origin_provenance.py.
    "ORIGIN_PUBLIC_URL": "https://forgejo.example-corp.dev",
    "ORIGIN_INTERNAL_HOST_ALIASES": "forgejo.internal.example-corp.dev",
    # Secondary construction surfaces (topology-driven, not trust anchors).
    "PULSE_ORIGIN_PUBLIC_URL": "https://forgejo.example-corp.dev",
    "PULSE_ORIGIN_INTERNAL_HTTP_URL": "http://forgejo.internal.example-corp.dev",
    "PULSE_CI_LOGS_BASE_URL": "http://ci-logs.internal.example-corp.dev:8080",
    "PULSE_BOOKS_URL": "http://books.internal.example-corp.dev:8021",
    "PULSE_ALERTMANAGER_URL": "http://alertmanager.internal.example-corp.dev:9093",
    "PULSE_PROMETHEUS_URL": "http://prometheus.internal.example-corp.dev:9090",
}
THIRD_PARTY = {
    "public": THIRD_PARTY_ENV["PULSE_ORIGIN_PUBLIC_URL"],
    "internal_http": THIRD_PARTY_ENV["PULSE_ORIGIN_INTERNAL_HTTP_URL"],
    "ci_logs": THIRD_PARTY_ENV["PULSE_CI_LOGS_BASE_URL"],
}


# Boot-required env the CI test job does not carry: tasks.apps.ready() aborts
# boot without an explicit substrate-cap mode (capability_evidence gate). The
# probes boot full Django, so they must supply it themselves.
_BOOT_ENV = {"PULSE_SUBSTRATE_CAP_MODE": "off"}


def _run_probe(env: dict[str, str], probe: str) -> dict:
    """Run a python probe with the given env in a fresh interpreter."""
    full_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PULSE_") and not k.startswith("ORIGIN_")
    }
    full_env.update(_BOOT_ENV)
    full_env.update(env)
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, env=full_env, timeout=60, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}\n{result.stdout}"
    return json.loads(result.stdout)


def _build_probe(hosts: dict[str, str]) -> str:
    """Probe built from the deployment's own hosts — never hardcoded literals."""
    return rf"""
import json
from tasks import origin_provenance as op
from taskmanager.topology import ci_logs_url, current_topology
public = {hosts['public']!r}
internal_http = {hosts['internal_http']!r}
t = current_topology()
print(json.dumps({{
    "canonical_run_relative": op.canonical_origin_run_url("/shizuha-labs/pulse/actions/runs/1"),
    "canonical_run_internal": op.canonical_origin_run_url(
        internal_http + "/shizuha-labs/pulse/actions/runs/7"),
    "canonical_pr": op.canonical_origin_pr_url("shizuha-labs", "pulse", 591),
    "pr_parse": op.parse_origin_pr_reference(
        public + "/shizuha-labs/pulse/pulls/591"),
    "legacy_singular_parse": bool(op._ORIGIN_PR_LEGACY_SINGULAR_URL_RE.match(
        public + "/shizuha-labs/pulse/pull/591")),
    "run_url_parse": bool(op.ORIGIN_RUN_URL_RE.search(
        "see " + public + "/shizuha-labs/pulse/actions/runs/21021 for details")),
    "ci_logs": ci_logs_url("shizuha-labs/pulse", 21021),
    "malformed_msg": op._malformed_origin_pr_url_error() if hasattr(op, "_malformed_origin_pr_url_error") else "",
    "topology_public": t.origin_public_url,
}}))
"""


def _django_setup_prelude() -> str:
    return (
        "import os\n"
        "if os.environ.get('DJANGO_SETTINGS_MODULE'):\n"
        "    import django\n"
        "    django.setup()\n"
    )


class Pls1409TopologyConfigifyTests(SimpleTestCase):
    databases: frozenset[str] = frozenset()  # subprocess probes only; no DB

    def test_defaults_preserve_first_party_topology(self):
        """Unconfigured: every built URL/regex still names first-party endpoints."""
        out = _run_probe(FIRST_PARTY_ENV, _build_probe(FIRST_PARTY))
        self.assertEqual(
            out["canonical_run_relative"],
            "https://origin.shizuha.com/shizuha-labs/pulse/actions/runs/1",
        )
        self.assertEqual(
            out["canonical_run_internal"],
            "https://origin.shizuha.com/shizuha-labs/pulse/actions/runs/7",
        )
        self.assertEqual(out["canonical_pr"], "https://origin.shizuha.com/shizuha-labs/pulse/pulls/591")
        # JSON round-trips tuples as arrays.
        self.assertEqual(out["pr_parse"], ["shizuha-labs", "pulse", 591])
        self.assertIs(out["legacy_singular_parse"], True)
        self.assertIs(out["run_url_parse"], True)
        self.assertTrue(out["ci_logs"].startswith("http://ci-logs.origin.svc.cluster.local:8080/logs?"))
        self.assertEqual(out["topology_public"], "https://origin.shizuha.com")

    def test_env_only_override_produces_no_first_party_urls(self):
        """Third-party env: no required path assumes origin.shizuha.com or cluster-local DNS."""
        out = _run_probe(THIRD_PARTY_ENV, _build_probe(THIRD_PARTY))
        blob = json.dumps(out)
        self.assertNotIn("origin.shizuha.com", blob)
        self.assertNotIn("svc.cluster.local", blob)
        self.assertEqual(out["canonical_pr"], "https://forgejo.example-corp.dev/shizuha-labs/pulse/pulls/591")
        self.assertEqual(out["pr_parse"], ["shizuha-labs", "pulse", 591])
        self.assertIs(out["legacy_singular_parse"], True)  # singular shape still identity-matched on THEIR host
        self.assertIs(out["run_url_parse"], True)
        self.assertEqual(
            out["canonical_run_internal"],
            "https://forgejo.example-corp.dev/shizuha-labs/pulse/actions/runs/7",
        )
        self.assertTrue(out["ci_logs"].startswith("http://ci-logs.internal.example-corp.dev:8080/logs?"))

    def test_first_party_hosts_fail_closed_under_third_party_topology(self):
        """A third-party deployment must not treat first-party hosts as trust anchors."""
        # Feed the first-party fixture URLs into a third-party process: parsing
        # them must fail closed (None/False), never match on their topology.
        out = _run_probe(THIRD_PARTY_ENV, _build_probe(FIRST_PARTY))
        self.assertIsNone(out["pr_parse"])
        self.assertIs(out["legacy_singular_parse"], False)
        self.assertIs(out["run_url_parse"], False)

    def test_company_state_defaults_are_env_configurable(self):
        """company_state source URLs come from topology, env-overridable, prod defaults intact."""
        prelude = _django_setup_prelude()
        default_out = _run_probe(
            {},
            prelude
            + "import json\n"
            "from tasks.services import company_state as cs\n"
            "print(json.dumps({k: getattr(cs, k) for k in "
            "['DEFAULT_ALERTMANAGER_URL', 'DEFAULT_PROMETHEUS_URL', 'DEFAULT_BOOKS_URL']}))",
        )
        self.assertEqual(default_out["DEFAULT_BOOKS_URL"], "http://shizuha-books.shizuha.svc.cluster.local:8021")
        self.assertTrue(default_out["DEFAULT_ALERTMANAGER_URL"].endswith(":9093"))
        override_out = _run_probe(
            THIRD_PARTY_ENV,
            prelude
            + "import json\n"
            "from tasks.services import company_state as cs\n"
            "print(json.dumps({k: getattr(cs, k) for k in "
            "['DEFAULT_ALERTMANAGER_URL', 'DEFAULT_PROMETHEUS_URL', 'DEFAULT_BOOKS_URL']}))",
        )
        self.assertNotIn("svc.cluster.local", json.dumps(override_out))
        self.assertEqual(override_out["DEFAULT_BOOKS_URL"], "http://books.internal.example-corp.dev:8021")

    def test_topology_defaults_are_documented_and_reloaded(self):
        """reload_topology() re-reads env. An explicit empty value does not restore the first-party default."""
        probe = (
            "import json, os\n"
            "from taskmanager import topology\n"
            "os.environ['PULSE_BOOKS_URL'] = 'http://books.tenant-a.example:8021'\n"
            "reloaded = topology.reload_topology()\n"
            "os.environ['PULSE_BOOKS_URL'] = ''\n"
            "os.environ['PULSE_ORIGIN_PUBLIC_HOST_ALIASES'] = ''\n"
            "os.environ['PULSE_GX10_HOST_PREFIX'] = ''\n"
            "os.environ['PULSE_TAILSCALE_CGNAT'] = ''\n"
            "cleared = topology.reload_topology()\n"
            "print(json.dumps({\n"
            "  'reloaded': reloaded.books_url,\n"
            "  'cleared': cleared.books_url,\n"
            "  'aliases': list(cleared.origin_public_host_aliases),\n"
            "  'gx10': cleared.gx10_host_prefix,\n"
            "  'tailscale': cleared.tailscale_cgnat,\n"
            "}))"
        )
        out = _run_probe({}, probe)
        self.assertEqual(out["reloaded"], "http://books.tenant-a.example:8021")
        self.assertEqual(out["cleared"], "")
        self.assertEqual(out["aliases"], [])
        self.assertEqual(out["gx10"], "")
        self.assertEqual(out["tailscale"], "")
