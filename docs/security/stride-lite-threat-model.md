# STRIDE-lite Threat Model — required for trust-boundary designs (PLAT-1247 / PLAT-1253)

A design/HLD is **trust-boundary-affecting** when it introduces or changes any of:

- an external surface (public endpoint, webhook, DM/channel boundary)
- a cross-org / cross-tenant data or contact path
- an auth / permission / role change (OAuth, SSO, token issuance, credentials)
- a new third-party or internal integration
- an admin / control-plane path
- data export / import

Trust-boundary designs **cannot reach approved/done/awaiting_merge** (any workflow)
without BOTH of the following. Enforcement is transition-time in
`WorkflowService._validate_stride_gate` (no sweep); kill-switch
`PULSE_STRIDE_HLD_GATE_ENABLED` (default on).

## 1. Threat Model section (in the task description or a comment)

Copy this template. The validator requires the "Threat Model" heading, all six
STRIDE categories, the words "trust boundary", and "mitigation" (or "residual
risk") to be present.

```markdown
## Threat Model (STRIDE-lite)

**Assets:** <what is worth stealing/breaking>
**Trust boundaries:** <where untrusted meets trusted — draw the line(s)>
**Data flows:** <who sends what to whom, over which channel>

| STRIDE category | Risk here? | Mitigation |
|---|---|---|
| Spoofing | … | … |
| Tampering | … | … |
| Repudiation (audit) | … | … |
| Information Disclosure | … | … |
| Denial of Service / abuse | … | … |
| Elevation of Privilege | … | … |

**Residual risk:** <what remains accepted, and why>
**Test/monitoring evidence expected:** <what proves the mitigations work in prod>
```

## 2. Security sign-off

Route the design through **Submit for Security Review**. The security reviewer
records approval with a comment containing `Security sign-off: approved`
(accepted variants: granted/passed/ok/lgtm). Without it the gate blocks
approval/close.

**Only a comment authored by a Security-team member counts.** The gate verifies
the sign-off against the commenter's server-stamped `author_email` (the roster
`security` team), which a non-security user cannot forge. Client-writable fields
carry no authenticated author and are therefore NOT accepted as sign-off — a
`Security sign-off: approved` comment from a non-security author, the
`security-signed-off` label, and a `metadata.security_signoff` value all fail the
gate (PLAT-1253). A machine-recorded sign-off must be posted as a Security-authored
comment (or a future cryptographically-signed token), not a plain label/metadata
value.

## Classification & opt-out

- Explicit labels always gate: `trust-boundary`, `security-sensitive`,
  `external-surface`, `cross-org`, `cross-tenant`, `auth`, `authz`,
  `permission-change`, `control-plane`, `webhook`, `data-export`, `data-import`.
- Text heuristics also gate (CON-129 class: "external onboarding", "outbound
  DM", "cross-org contact path", …). If the heuristic misfires on a
  non-trust-boundary design, leave an auditable comment
  `stride: N/A — <reason>`. **Label-classified designs cannot be waived.**

## Worked check case (CON-129)

The Hive external-onboarding / Connect-DM-boundary design qualifies (external
users + cross-org contact + outbound DMs). Its threat model must surface the
cross-org-contact risk and require mitigations such as shared-org
authorization, external-user guardrails, test-account-only QA, and
audit/anomaly detection — exactly the incident the 2026-07-02 CON-129 DM to an
external user would have been prevented by.

Origin: PLAT-1247 (Security Program 1), PLAT-1235; implementation PLAT-1253.
