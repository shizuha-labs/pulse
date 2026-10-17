# Runbook: Forgejo Security CI (PLAT-1248)

This repository runs `.forgejo/workflows/security-ci.yml` on PRs, pushes to `main`/`master`, and manual dispatch. The gate is free/self-hosted and does not require Semgrep AppSec Platform or paid GitHub code scanning.

## What runs

- **Semgrep SAST**: `semgrep scan --config p/ci --json --output semgrep.json .` for broad JS/Python/static rules.
- **Bandit SAST**: `bandit -r . -f json -o bandit.json ...` for Python-specific security checks.
- **OSV-Scanner SCA**: `google/osv-scanner-action` recursively scans manifests/lockfiles and emits `osv.json`.
- **Trivy SCA**: `aquasecurity/trivy-action` scans the repo filesystem for HIGH/CRITICAL vulnerable dependencies and emits `trivy.json`.
- **Durable ledger + Pulse handoff**: `scripts/security-ci-to-pulse.py` normalizes all JSON outputs, maps severity to Pulse priority, and consumes stable `security-ci:<hash>` allowlist entries. In rollup mode it first upserts the repo's derived page under Wiki `SEC`; only a successful Wiki write may produce a page link in the rolling Pulse task. Missing space/auth/write failures instead add the bounded `durable-write-failed` signal to that one task routed to Security. The checked-in `.security-ci-allowlist.json` remains the machine-consumed suppression authority; Wiki is a human-readable projection.

If Pulse secrets are unavailable (for example on an untrusted fork PR), the job still prints and writes `security-ci-summary.md`; active findings at or above `SECURITY_CI_FAIL_ON` fail the gate.

The trusted Origin workflow supplies `WIKI_API_URL`, `WIKI_TOKEN`, and `WIKI_ORGANIZATION_ID`. The Wiki token is the security-ci Shizuha-ID service identity; never configure a separate unaudited user token.

## Severity mapping

| Scanner signal | Normalized severity | Pulse priority |
|---|---:|---:|
| `critical` / CVSS >= 9.0 | `critical` | `urgent` |
| `high` / Semgrep `ERROR` / CVSS >= 7.0 | `high` | `high` |
| `medium`, `moderate`, Semgrep `WARNING`, CVSS >= 4.0 | `medium` | `normal` |
| `low`, `info`, Semgrep `INFO`/`NOTE` | `low`/`info` | `low` |

Default gate threshold: `SECURITY_CI_FAIL_ON=high`.

## Suppression / allowlist procedure

Suppress only confirmed false positives or accepted-risk findings. Add `.security-ci-allowlist.json` (copy `.security-ci-allowlist.example.json`) and include:

- the stable `source_id` from CI output whenever possible;
- `tool`, `rule`, and optional `path_contains` for scoped matching;
- a human-readable `reason` and an accountable `owner`.

Never blanket-suppress a tool or broad path. Security reviews the resulting `security-finding` task and closes it through the security workflow when remediated or accepted as a false positive.

## Local dry-run

```bash
python3 scripts/security-ci-to-pulse.py \
  --semgrep semgrep.json \
  --bandit bandit.json \
  --osv osv.json \
  --trivy trivy.json \
  --dry-run \
  --fail-on high
```

## Official references checked

- Forgejo Actions workflows live under `.forgejo/workflows` and are executed by runners, not the Forgejo server.
- Semgrep CLI supports CI/static scans and JSON output.
- Bandit supports JSON output with severity/confidence fields.
- OSV-Scanner scans directories/manifests/lockfiles and returns a failing status when vulnerabilities are found.
- Trivy filesystem scans emit JSON vulnerability results.
