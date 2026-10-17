"""PLS-1370 / PLS-1371 disposition binding repair command."""

from __future__ import annotations

import io
import json
import uuid
from contextlib import redirect_stdout

from django.core.management import call_command
from django.test import TestCase

from tasks.models import EscalationRoutingDisposition, Item, Project, TeamRoster
from tasks.services.external_wait import HUMAN_CLASSES
from tasks.tests.base import create_blocked_item


def _mk_project():
    return Project.objects.create(
        name='PLS-1370 Repair', key='R1370', org_id=1, access_level='open',
    )


def _ensure_roster(project, email):
    TeamRoster.objects.get_or_create(
        organization_id=project.org_id,
        team_name='test-fixtures',
        defaults={'organization_slug': 'shizuha', 'tiers': [[email]]},
    )


def _mk_item(project, key, status, *, tags='PLAT-4215', email='holder@example.com'):
    _ensure_roster(project, email)
    kwargs = dict(
        project=project, item_key=key, title=key, mode='task',
        workflow_name='simple', tags=tags, assignee_email=email,
        created_by_email='kumo@shizuha.com', created_by_id=0,
    )
    if status == 'blocked':
        return create_blocked_item(**kwargs)
    return Item.objects.create(status=status, **kwargs)


def _human_evidence():
    return {
        'schema': 'shizuha.pulse.escalation-evidence.v1',
        'dependency': {
            'evidence_class': next(iter(HUMAN_CLASSES)),
            'holder_ref': 'operator@example.com',
            'requested_subject': 'run auth test',
            'acceptance_check': 'session green',
        },
    }


def _mk_disp(project, parent, child, *, generation=1, classification='operator_held_dependency',
             outcome='routed', evidence=None, digest=None):
    return EscalationRoutingDisposition.objects.create(
        organization_id=project.org_id,
        project=project,
        item=parent,
        child_item=child,
        transition_id=uuid.uuid4(),
        source_generation=generation,
        generation=generation,
        disposition_digest=digest or f'digest-{child.item_key}-{generation}',
        schema_version='shizuha.pulse.escalation-evidence.v1',
        classification=classification,
        outcome=outcome,
        reason_code='fixture',
        normalized_evidence=evidence if evidence is not None else _human_evidence(),
        pre_state={},
        final_state={},
    )


def _run(**kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        call_command('repair_pls835_disposition_bindings', format='json', **kwargs)
    return json.loads(buf.getvalue())


class Pls1370CanonicalizeTests(TestCase):
    def test_canonicalize_mixed_held_with_complete_limb(self):
        project = _mk_project()
        parent = _mk_item(project, 'R1370-P1', 'blocked')
        # Use a frozen instrument key so the command accepts it.
        child = _mk_item(project, 'VEN-258', 'todo')
        disp = _mk_disp(
            project, parent, child,
            classification='mixed_or_unknown_escalation_hold',
            outcome='held',
            evidence=_human_evidence(),
        )
        payload = _run(mode='instrument', keys='VEN-258', execute=True)
        audit = payload['audits'][0]
        self.assertTrue(any(a.startswith('canonicalized:') for a in audit['applied']))
        disp.refresh_from_db()
        self.assertEqual(disp.classification, 'operator_held_dependency')
        self.assertEqual(disp.outcome, 'routed')

    def test_rejected_is_not_canonicalized(self):
        project = _mk_project()
        parent = _mk_item(project, 'R1370-P2', 'blocked')
        child = _mk_item(project, 'PLAT-6762', 'todo')
        disp = _mk_disp(
            project, parent, child,
            classification='operator_request_hygiene_reject',
            outcome='rejected',
            evidence=_human_evidence(),
        )
        payload = _run(mode='instrument', keys='PLAT-6762', execute=True)
        audit = payload['audits'][0]
        self.assertTrue(any('needs_closure_review' in b for b in audit['blocked']))
        disp.refresh_from_db()
        self.assertEqual(disp.classification, 'operator_request_hygiene_reject')


class Pls1371DedupeTests(TestCase):
    def test_dedupe_retires_non_originating_bindings(self):
        project = _mk_project()
        parent = _mk_item(project, 'R1370-P3', 'blocked')
        child = _mk_item(project, 'MAIL-124', 'todo')
        keep = _mk_disp(
            project, parent, child, generation=1,
            classification='operator_held_dependency', outcome='routed',
            digest='keep-1',
        )
        dup = _mk_disp(
            project, parent, child, generation=2,
            classification='mixed_or_unknown_escalation_hold', outcome='held',
            digest='dup-2',
        )
        payload = _run(mode='dedupe', keys='MAIL-124', execute=True)
        audit = payload['audits'][0]
        self.assertTrue(any(a.startswith('retired_binding:') for a in audit['applied']))
        keep.refresh_from_db()
        dup.refresh_from_db()
        self.assertEqual(keep.child_item_id, child.pk)
        self.assertIsNone(dup.child_item_id)
        self.assertEqual(
            EscalationRoutingDisposition.objects.filter(child_item=child).count(),
            1,
        )

    def test_execute_does_not_retire_without_authority_bound_row(self):
        project = _mk_project()
        parent = _mk_item(project, 'R1371-P4', 'blocked')
        # The command refuses keys outside the frozen PLS-1370/1371 sets
        # (CommandError), so the no-authority-bound-row guard must be exercised
        # on a frozen key. MAIL-124 is in DUPLICATE_KEYS; per-test transactions
        # keep fixture reuse across tests isolated.
        child = _mk_item(project, 'MAIL-124', 'todo')
        first = _mk_disp(
            project, parent, child, generation=1,
            classification='mixed_or_unknown_escalation_hold', outcome='held',
            digest='no-auth-1',
        )
        second = _mk_disp(
            project, parent, child, generation=2,
            classification='mixed_or_unknown_escalation_hold', outcome='held',
            digest='no-auth-2',
        )
        payload = _run(mode='dedupe', keys='MAIL-124', execute=True)
        audit = payload['audits'][0]
        self.assertIn('no_authority_bound_originating_row', audit['blocked'])
        self.assertFalse(any(a.startswith('retired_binding:') for a in audit['applied']))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.child_item_id, child.pk)
        self.assertEqual(second.child_item_id, child.pk)
