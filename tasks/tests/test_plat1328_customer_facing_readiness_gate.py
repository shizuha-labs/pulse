"""PLAT-1328: preventive external-exposure readiness sign-off gate.

Parent PLAT-1318 (CISO preventive gate). External exposure must be blocked at the
transition (event-driven, no sweep) unless a Security-team-authored, non-stale
readiness sign-off comment exists — or an audited internal-only N/A waiver.

Mirrors the learning-loop gate test harness: build a transition carrying the
validator, a SimpleNamespace item with labels/tags/text/comments, and call
WorkflowService._run_validators directly, asserting block/allow.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

SECURITY_EMAIL = 'security-lead@shizuha.com'
ENG_EMAIL = 'kai@shizuha.com'


def _is_security_team_email(email):
    """Stand-in roster: only the security lead is on the Security team."""
    return (email or '').strip().lower() == SECURITY_EMAIL


def _label_activity(old='', new=''):
    """An ItemActivity 'Labels' history row (see signals._format_value: labels
    render as 'key=val, key2=val2'). Used to prove PLAT-4021 Defect 1 sticky
    classification — a label present in history keeps the gate armed after removal."""
    return SimpleNamespace(field_name='Labels', old_value=old, new_value=new)


def _signoff(probe_days_ago=3, expires_in_days=27, missing=(), approved=True):
    """Build a Customer-facing readiness sign-off body. `missing` drops fields."""
    today = timezone.now().date()
    probe = today - datetime.timedelta(days=probe_days_ago)
    expiry = today + datetime.timedelta(days=expires_in_days)
    lines = []
    lines.append('Customer-facing readiness: approved' if approved
                 else 'Customer-facing readiness: pending')
    fields = {
        'project/surface': 'Project/surface: HIVE / https://hive.shizuha.com',
        'isolation epic': 'Isolation epic: HIVE-354 (done)',
        'authz ci': 'Authz CI: https://origin/run/981 SHA abc123 green',
        'live cross-tenant probe': f'Live cross-tenant probe: SEC-42 {probe.isoformat()} pass',
        'scope/expiry': f'Scope/expiry: hosted claw egress; expires {expiry.isoformat()}',
    }
    for k, line in fields.items():
        if k not in missing:
            lines.append(line)
    return '\n'.join(lines)


class ExternalReadinessGateTests(SimpleTestCase):
    def _transition(self, **cfg):
        return SimpleNamespace(
            validators=[{
                'type': 'customer_facing_readiness_signoff',
                **cfg,
            }],
            from_status='in_review',
            to_status='approved',
        )

    def _item(self, *, labels=None, tags='', title='Launch', description='', comments=None,
              activities=None):
        return SimpleNamespace(
            id=1, item_key='PLAT-9999', title=title, description=description,
            labels=labels or [], tags=tags, comments=comments or [],
            activities=activities or [],
        )

    def _comment(self, content, author_email=SECURITY_EMAIL):
        return SimpleNamespace(content=content, author_email=author_email)

    def _run(self, item, transition=None):
        with patch.object(WorkflowService, '_is_security_team_email', side_effect=_is_security_team_email), \
             patch.object(WorkflowService, '_alert_security_team', return_value=None):
            WorkflowService._run_validators(transition or self._transition(), item)

    # --- gate applicability -------------------------------------------------
    def test_non_external_item_passes(self):
        """A task that is not external-exposure is not gated."""
        self._run(self._item(labels=['backend'], title='internal refactor'))

    def test_classifies_by_label(self):
        item = self._item(labels=['external-exposure'])
        with self.assertRaisesRegex(WorkflowValidationError, 'PLAT-1328'):
            self._run(item)

    def test_text_only_no_longer_arms_gate_plat4001(self):
        """PLAT-4001: title/description trigger wording WITHOUT an explicit exposure
        label no longer arms the hard gate — it merely *mentions* externality. This is
        the KURA-97 / PLAT-3200 mis-fire class (internal work forced to get a Security
        N/A waiver just for quoting the phrases). Text is now a soft hint only."""
        item = self._item(title='Onboard design partner',
                          description='Enable this surface for an external customer / design partner.')
        self._run(item)  # must NOT raise: no explicit exposure label -> gate not armed

    # --- PLAT-4001: mis-fire suppressed, true-positive preserved ------------
    def test_internal_doc_with_trigger_text_passes(self):
        """A wiki/doc edit that merely quotes trigger phrases (e.g. editing the
        Security-Coverage-Program page) closes WITHOUT a Security waiver — PLAT-3200."""
        item = self._item(
            labels=['docs', 'no-pr'],
            title='Update Security-Coverage-Program wiki page',
            description='Document the customer-facing readiness gate and external exposure policy.',
        )
        self._run(item)  # no raise

    def test_internal_decision_tracker_passes(self):
        """A CoS decision-tracker whose OUTCOME is 'stays internal' closes without a
        waiver, even though it discusses per-tenant / customer-facing scope — KURA-97
        (the irony case: a decision whose outcome is 'not external' cannot be Done
        because it *mentions* externality)."""
        item = self._item(
            tags='security-sensitive',
            title='[CoS decision] per-tenant backup scope',
            description='Decision: Kura stays internal-single-tenant, NOT externally exposed. '
                        'Do not build per-tenant customer-facing isolation now.',
        )
        self._run(item)  # no raise

    def test_genuine_external_exposure_still_gates(self):
        """True-positive preserved: an explicit exposure label still arms the gate and
        blocks close without a Security sign-off."""
        with self.assertRaisesRegex(WorkflowValidationError, 'PLAT-1328'):
            self._run(self._item(labels=['external-exposure'],
                                 title='Expose hosted claw egress to customers'))

    # --- block cases --------------------------------------------------------
    def test_external_without_signoff_blocked(self):
        with self.assertRaisesRegex(WorkflowValidationError, 'no Security-team-authored'):
            self._run(self._item(labels=['customer-facing']))

    def test_non_security_authored_signoff_ignored(self):
        """A perfect sign-off authored by a non-Security member is not accepted."""
        c = self._comment(_signoff(), author_email=ENG_EMAIL)
        with self.assertRaisesRegex(WorkflowValidationError, 'no Security-team-authored'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_stale_probe_blocked(self):
        c = self._comment(_signoff(probe_days_ago=45))
        with self.assertRaisesRegex(WorkflowValidationError, 'stale'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_missing_evidence_field_blocked(self):
        c = self._comment(_signoff(missing=('authz ci',)))
        with self.assertRaisesRegex(WorkflowValidationError, 'missing required field'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_todo_blocked(self):
        body = _signoff().replace('Authz CI: https://origin/run/981 SHA abc123 green',
                                  'Authz CI: TODO')
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'Authz CI'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_negative_status_overrides_positive_token(self):
        body = _signoff().replace('Authz CI: https://origin/run/981 SHA abc123 green',
                                  'Authz CI: https://origin/run/981 SHA abc123 failed but green later')
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'not fail'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_negated_green_status_blocked(self):
        body = _signoff().replace('Authz CI: https://origin/run/981 SHA abc123 green',
                                  'Authz CI: https://origin/run/981 SHA abc123 not green')
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'negated status'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_negated_passed_status_blocked(self):
        body = _signoff().replace('Authz CI: https://origin/run/981 SHA abc123 green',
                                  'Authz CI: https://origin/run/981 SHA abc123 not passed')
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'negated status'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_probe_fresh_date_but_fail_result_blocked(self):
        today = timezone.now().date().isoformat()
        body = _signoff().replace(
            f'Live cross-tenant probe: SEC-42 {timezone.now().date() - datetime.timedelta(days=3)} pass',
            f'Live cross-tenant probe: SEC-42 {today} fail',
        )
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'pass result'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_scope_without_expiry_date_blocked(self):
        body = _signoff().replace(
            f"Scope/expiry: hosted claw egress; expires {(timezone.now().date() + datetime.timedelta(days=27)).isoformat()}",
            'Scope/expiry: hosted claw egress; expires TBD',
        )
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'explicit YYYY-MM-DD expiry'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_expired_scope_blocked(self):
        c = self._comment(_signoff(expires_in_days=-1))
        with self.assertRaisesRegex(WorkflowValidationError, 'expired'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_not_approved_signoff_blocked(self):
        c = self._comment(_signoff(approved=False))
        with self.assertRaises(WorkflowValidationError):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    # --- pass cases ---------------------------------------------------------
    def test_valid_security_signoff_passes(self):
        c = self._comment(_signoff())
        self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_probe_exactly_30_days_passes(self):
        c = self._comment(_signoff(probe_days_ago=30))
        self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_internal_only_na_waiver_passes(self):
        """A Security-authored N/A waiver passes for internal-only scope."""
        c = self._comment('external-readiness: N/A — internal-only tooling, no customer surface')
        self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_security_na_waiver_without_internal_scope_blocks(self):
        """Security-authored N/A waivers must prove the work is internal-only."""
        c = self._comment('external-readiness: N/A — trust me')
        with self.assertRaisesRegex(WorkflowValidationError, 'internal-only/no-customer/no-external'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_na_waiver_from_non_security_does_not_pass(self):
        """A non-Security N/A waiver is not an audited waiver → still blocked."""
        c = self._comment('external-readiness: N/A — trust me', author_email=ENG_EMAIL)
        with self.assertRaises(WorkflowValidationError):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    # --- PLAT-4021 Defect 1: sticky classification (label-removal bypass) ----
    def test_label_removed_still_armed_via_history(self):
        """A genuinely-external task cannot be self-cleared by removing the
        `external-exposure` label right before the close transition — the label's
        presence in ItemActivity history keeps the gate armed."""
        item = self._item(
            labels=[],  # label silently removed pre-close
            title='Expose hosted claw egress to customers',
            activities=[_label_activity(old='external-exposure=True, backend=True',
                                        new='backend=True')],
        )
        with self.assertRaisesRegex(WorkflowValidationError, 'PLAT-1328'):
            self._run(item)

    def test_security_cleared_comment_dearms(self):
        """De-arming is allowed ONLY via a Security-team-authored
        `external-exposure: cleared — <reason>` comment (auditable reclassification)."""
        item = self._item(
            labels=['external-exposure'],
            comments=[self._comment('external-exposure: cleared — confirmed mislabeled; '
                                    'internal-only tooling, no customer surface')],
        )
        self._run(item)  # no raise: Security cleared → gate not armed

    def test_cleared_comment_from_non_security_ignored(self):
        """A non-Security `cleared` comment cannot de-arm the gate — no self-clear."""
        item = self._item(
            labels=['external-exposure'],
            comments=[self._comment('external-exposure: cleared — trust me',
                                    author_email=ENG_EMAIL)],
        )
        with self.assertRaisesRegex(WorkflowValidationError, 'no Security-team-authored'):
            self._run(item)

    def test_history_armed_dearmed_by_security_cleared(self):
        """History-armed (label removed) + Security `cleared` comment → de-armed."""
        item = self._item(
            labels=[],
            activities=[_label_activity(old='customer-facing=True', new='')],
            comments=[self._comment('external-exposure: cleared — reclassified internal-only '
                                    'after review, no external customer surface')],
        )
        self._run(item)  # no raise

    def test_cleared_comment_without_reason_does_not_dearm(self):
        """A `cleared` marker with no reason is not an auditable de-arm → still armed."""
        item = self._item(
            labels=['external-exposure'],
            comments=[self._comment('external-exposure: cleared —   ')],
        )
        with self.assertRaisesRegex(WorkflowValidationError, 'PLAT-1328'):
            self._run(item)

    # --- PLAT-4021 Defect 2: latest Security sign-off is authoritative -------
    def test_latest_valid_signoff_clears_after_earlier_malformed(self):
        """Oldest malformed field-block (Authz CI missing) + newest VALID field-block
        → gate passes (PLAT-4005 / HIVE-563: a corrected re-post must clear)."""
        malformed = self._comment(_signoff(missing=('authz ci',)))
        valid = self._comment(_signoff())
        self._run(self._item(labels=['external-exposure'], comments=[malformed, valid]))

    def test_latest_clean_na_clears_after_malformed_fieldblock(self):
        """Oldest malformed field-block + newest clean internal-only N/A line → passes."""
        malformed = self._comment(_signoff(missing=('authz ci',)))
        clean_na = self._comment('external-readiness: N/A — internal-only tooling, no customer surface')
        self._run(self._item(labels=['external-exposure'], comments=[malformed, clean_na]))

    def test_valid_signoff_still_clears_when_newer_malformed_coexists(self):
        """Regression: any valid sign-off clears even if a NEWER malformed one exists —
        the latest-wins change must not make the gate stricter than 'a valid sign-off
        exists'."""
        valid = self._comment(_signoff())
        malformed = self._comment(_signoff(missing=('authz ci',)))
        self._run(self._item(labels=['external-exposure'], comments=[valid, malformed]))

    # --- fail-loud ----------------------------------------------------------
    def test_block_emits_fail_loud_alert(self):
        """A block calls the Security-team alert path (PLAT-1254 fail-loud)."""
        item = self._item(labels=['external-exposure'])
        with patch.object(WorkflowService, '_is_security_team_email', side_effect=_is_security_team_email), \
             patch.object(WorkflowService, '_alert_security_team', return_value=None) as alert:
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(self._transition(), item)
            self.assertTrue(alert.called, 'gate block must fire the fail-loud Security alert')

    # --- PLAT-4025: N/A — no CI author-side evidence form -------------------

    def _no_ci_signoff(self, sha='aa4acd8e9669e7b2537eb05ff08a83b9a32816c9', result='5 tests OK'):
        return _signoff().replace(
            'Authz CI: https://origin/run/981 SHA abc123 green',
            f'Authz CI: N/A — no CI; sha={sha} — {result}',
        )

    def test_authz_ci_no_ci_fixture_passes(self):
        """Canonical N/A fixture from PLAT-4025 contract decision must pass the gate."""
        c = self._comment(self._no_ci_signoff())
        self._run(self._item(labels=['external-exposure'], comments=[c]))  # no raise

    def test_authz_ci_no_ci_missing_sha_blocked(self):
        """N/A form without sha=<hex> must still be blocked — SHA is always required."""
        body = _signoff().replace(
            'Authz CI: https://origin/run/981 SHA abc123 green',
            'Authz CI: N/A — no CI; 5 tests OK',
        )
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'SHA'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_no_ci_fail_result_blocked(self):
        """N/A form with a fail indicator must be blocked."""
        c = self._comment(self._no_ci_signoff(result='3 tests failed'))
        with self.assertRaisesRegex(WorkflowValidationError, 'fail'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_no_ci_no_result_blocked(self):
        """N/A form with no green/pass/OK result must be blocked."""
        body = _signoff().replace(
            'Authz CI: https://origin/run/981 SHA abc123 green',
            f'Authz CI: N/A — no CI; sha=aa4acd8e9669e7b2537eb05ff08a83b9a32816c9 — ran locally',
        )
        c = self._comment(body)
        with self.assertRaisesRegex(WorkflowValidationError, 'green/pass result'):
            self._run(self._item(labels=['external-exposure'], comments=[c]))

    def test_authz_ci_no_ci_ok_keyword_passes(self):
        """'OK' is accepted as a green indicator in the N/A form."""
        c = self._comment(self._no_ci_signoff(result='17 tests OK'))
        self._run(self._item(labels=['external-exposure'], comments=[c]))  # no raise

    def test_authz_ci_no_ci_pass_keyword_passes(self):
        """'pass' is accepted as a green indicator in the N/A form."""
        c = self._comment(self._no_ci_signoff(result='all pass'))
        self._run(self._item(labels=['external-exposure'], comments=[c]))  # no raise

    def test_authz_ci_standard_url_form_still_passes(self):
        """The standard CI-URL form must continue to work after the N/A addition."""
        c = self._comment(_signoff())
        self._run(self._item(labels=['external-exposure'], comments=[c]))  # no raise
