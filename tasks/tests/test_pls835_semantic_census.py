"""PLS-835 gen-1 census: focus predicates + authority ambiguity (ryo/CoS)."""

from __future__ import annotations

import io
import json
import uuid
from contextlib import redirect_stdout

from django.core.management import call_command
from django.test import TestCase

from tasks.management.commands.census_pls835_waiting_external import (
    _authority_verdict,
    _focus_limbs,
    _limb_verdict,
)
from tasks.models import (
    EscalationRoutingDisposition,
    ExternalWaitAuthority,
    Item,
    ItemLink,
    Project,
    TeamRoster,
)
from tasks.services.external_wait import HUMAN_CLASSES, admit_human_escalation
from tasks.tests.base import create_blocked_item


def _mk_project():
    return Project.objects.create(
        name='PLS-835 Census', key='C835', org_id=1, access_level='open',
    )


def _ensure_roster(project, assignee_email):
    """Seed a TeamRoster for the assignee so the PLS-539 org-mismatch
    assignment guard does not reject fixture rows (org_id=1 -> 'shizuha')."""
    if not assignee_email:
        return
    TeamRoster.objects.get_or_create(
        organization_id=project.org_id,
        team_name='test-fixtures',
        defaults={
            'organization_slug': 'shizuha',
            'tiers': [[assignee_email]],
        },
    )


def _mk_item(project, key, status, *, tags='', assignee_email='holder@example.com'):
    _ensure_roster(project, assignee_email)
    kwargs = dict(
        project=project,
        item_key=key,
        title=key,
        mode='task',
        workflow_name='simple',
        tags=tags,
        assignee_email=assignee_email,
        created_by_email='kumo@shizuha.com',
        created_by_id=0,
    )
    if status == 'blocked':
        # PLAT-4215: a blocked row needs a durable parent + active linked
        # blocker; create_blocked_item builds that graph in production order.
        return create_blocked_item(**kwargs)
    return Item.objects.create(status=status, **kwargs)


def _human_evidence():
    ec = next(iter(HUMAN_CLASSES))
    return {
        'schema': 'shizuha.pulse.escalation-evidence.v1',
        'dependency': {
            'evidence_class': ec,
            'holder_ref': 'operator@example.com',
            'requested_subject': 'run auth test',
            'acceptance_check': 'session green',
        },
    }


def _mk_disposition(project, parent, child, *, generation=1):
    return EscalationRoutingDisposition.objects.create(
        organization_id=project.org_id,
        project=project,
        item=parent,
        child_item=child,
        transition_id=uuid.uuid4(),
        source_generation=generation,
        generation=generation,
        disposition_digest=f'digest-{child.item_key}-{generation}',
        schema_version='shizuha.pulse.escalation-evidence.v1',
        classification='operator_held_dependency',
        outcome='routed',
        reason_code='validated_operator_held',
        normalized_evidence=_human_evidence(),
        pre_state={},
        final_state={},
    )


class Pls835LimbAndAuthorityUnitTests(TestCase):
    def test_limb_verdict_requires_human_fields(self):
        ok, msg = _limb_verdict(_human_evidence())
        self.assertTrue(ok)
        self.assertTrue(msg.startswith('pass:'))
        bad, reason = _limb_verdict({'dependency': {'evidence_class': 'nope'}})
        self.assertFalse(bad)
        self.assertIn('fail:', reason)

    def test_authority_none_is_not_ambiguous(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-P1', 'blocked')
        child = _mk_item(project, 'C835-C1', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child)
        v = _authority_verdict(child, disp)
        self.assertEqual(v['authority_class'], 'none')
        self.assertFalse(v['authority_ambiguous'])

    def test_authority_same_disposition_not_ambiguous(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-P2', 'blocked')
        child = _mk_item(project, 'C835-C2', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child)
        ExternalWaitAuthority.objects.create(
            item=child,
            organization_id=project.org_id,
            wait_generation=1,
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_WAITING,
            pre_status='todo',
            evidence={'disposition_id': str(disp.id)},
        )
        v = _authority_verdict(child, disp)
        self.assertEqual(v['authority_class'], 'same_disposition')
        self.assertTrue(v['authority_matches_disposition'])
        self.assertFalse(v['authority_ambiguous'])

    def test_authority_mismatched_disposition_is_ambiguous(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-P3', 'blocked')
        child = _mk_item(project, 'C835-C3', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child)
        ExternalWaitAuthority.objects.create(
            item=child,
            organization_id=project.org_id,
            wait_generation=1,
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_WAITING,
            pre_status='todo',
            evidence={'disposition_id': str(uuid.uuid4())},
        )
        v = _authority_verdict(child, disp)
        self.assertTrue(v['authority_ambiguous'])
        self.assertIn('disposition_mismatch', v['authority_ambiguity_reason'])

    def test_authority_missing_disposition_id_is_ambiguous(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-P4', 'blocked')
        child = _mk_item(project, 'C835-C4', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child)
        ExternalWaitAuthority.objects.create(
            item=child,
            organization_id=project.org_id,
            wait_generation=1,
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_ENTER_ARMED,
            pre_status='todo',
            evidence={},
        )
        v = _authority_verdict(child, disp)
        self.assertTrue(v['authority_ambiguous'])
        self.assertIn('missing_disposition_id', v['authority_ambiguity_reason'])

    def test_multiple_active_authorities_ambiguous(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-P5', 'blocked')
        child = _mk_item(project, 'C835-C5', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child)
        for gen in (1, 2):
            ExternalWaitAuthority.objects.create(
                item=child,
                organization_id=project.org_id,
                wait_generation=gen,
                branch=ExternalWaitAuthority.BRANCH_HUMAN,
                state=ExternalWaitAuthority.STATE_WAITING,
                pre_status='todo',
                evidence={'disposition_id': str(disp.id)},
            )
        v = _authority_verdict(child, disp)
        self.assertEqual(v['authority_class'], 'multiple_active')
        self.assertTrue(v['authority_ambiguous'])


class Pls835FocusAndCommandTests(TestCase):
    def test_intermediate_parent_with_upstream_is_not_focus_candidate(self):
        """MAIL-82 class: blocked by an active child leaf → not focus-eligible."""
        project = _mk_project()
        root = _mk_item(project, 'C835-ROOT', 'blocked')
        mid = _mk_item(project, 'C835-MID', 'blocked', tags='PLAT-4215')
        leaf = _mk_item(project, 'C835-LEAF', 'in_progress', tags='PLAT-4215')
        # leaf blocks mid; mid blocks root (canonical blocks orientation)
        ItemLink.objects.create(
            source_item=leaf, target_item=mid, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        ItemLink.objects.create(
            source_item=mid, target_item=root, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        mid_focus = _focus_limbs(mid)
        leaf_focus = _focus_limbs(leaf)
        self.assertTrue(mid_focus['has_active_upstream'])
        self.assertFalse(mid_focus['is_focus_candidate'])
        self.assertFalse(leaf_focus['has_active_upstream'])
        self.assertTrue(leaf_focus['has_active_blocked_descendant'])
        self.assertTrue(leaf_focus['is_focus_candidate'])

    def test_command_excludes_upstream_blocked_mid_and_flags_ambiguity(self):
        project = _mk_project()
        root = _mk_item(project, 'C835-R', 'blocked')
        mid = _mk_item(project, 'C835-M', 'blocked', tags='PLAT-4215')
        leaf = _mk_item(project, 'C835-L', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=leaf, target_item=mid, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        ItemLink.objects.create(
            source_item=mid, target_item=root, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, mid, leaf, generation=1)
        _mk_disposition(project, root, mid, generation=1)

        # Ambiguous authority on an unrelated eligible-shaped row
        other_parent = _mk_item(project, 'C835-OP', 'blocked')
        other = _mk_item(project, 'C835-O', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=other, target_item=other_parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        other_disp = _mk_disposition(project, other_parent, other, generation=1)
        ExternalWaitAuthority.objects.create(
            item=other,
            organization_id=project.org_id,
            wait_generation=1,
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_WAITING,
            pre_status='todo',
            evidence={'disposition_id': str(uuid.uuid4())},
        )

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload['schema'], 'shizuha.pulse.pls835-semantic-census.v3')
        self.assertFalse(payload['census_valid'])
        self.assertGreaterEqual(payload['ambiguity_row_count'], 1)
        # freeze keys suppressed while invalid
        self.assertEqual(payload['eligible_keys'], [])
        # mid must not be eligible (upstream leaf active)
        by_key = {r['child_key']: r for r in payload['rows']}
        self.assertIn('C835-M', by_key)
        self.assertFalse(by_key['C835-M']['eligible'])
        self.assertTrue(by_key['C835-M']['has_active_upstream'])
        self.assertFalse(by_key['C835-M']['is_focus_candidate'])
        # leaf is focus candidate; may appear in unvalidated list only
        self.assertTrue(by_key['C835-L']['is_focus_candidate'])
        self.assertTrue(by_key['C835-L']['eligible'])
        self.assertIn('C835-L', payload['eligible_keys_unvalidated'])
        self.assertNotIn('C835-M', payload['eligible_keys_unvalidated'])
        # other is ineligible + ambiguous
        self.assertFalse(by_key['C835-O']['eligible'])
        self.assertTrue(by_key['C835-O']['authority_ambiguous'])
        self.assertNotIn('C835-O', payload['eligible_keys_unvalidated'])
        del other_disp  # silence linters

    def test_missing_assignee_on_focus_leaf_invalidates_census(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-AP', 'blocked')
        child = _mk_item(
            project, 'C835-AU', 'todo', tags='PLAT-4215', assignee_email='',
        )
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, parent, child, generation=1)
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload['census_valid'])
        by_key = {r['child_key']: r for r in payload['rows']}
        self.assertIn('missing_assignee_email', by_key['C835-AU']['ineligible_reasons'])


    def test_malformed_limbs_invalidate_census(self):
        """rui P1: instrument contradiction must not silently shrink freeze set."""
        project = _mk_project()
        parent = _mk_item(project, 'C835-LP', 'blocked')
        child = _mk_item(project, 'C835-LC', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        disp = _mk_disposition(project, parent, child, generation=1)
        # Corrupt limbs after create
        disp.normalized_evidence = {'dependency': {'evidence_class': 'nope'}}
        disp.save(update_fields=['normalized_evidence'])

        # A second clean eligible-shaped row — freeze must still be suppressed.
        parent2 = _mk_item(project, 'C835-LP2', 'blocked')
        child2 = _mk_item(project, 'C835-LC2', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child2, target_item=parent2, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, parent2, child2, generation=1)

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload['census_valid'])
        self.assertEqual(payload['eligible_keys'], [])
        classes = {a.get('authority_class') for a in payload['ambiguity_rows']}
        self.assertIn('instrument_contradiction', classes)
        by_key = {r['child_key']: r for r in payload['rows']}
        self.assertFalse(by_key['C835-LC']['eligible'])
        self.assertTrue(
            any(x.startswith('fail:') for x in by_key['C835-LC']['ineligible_reasons'])
        )

    def test_malformed_classification_invalidates_census(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-CP', 'blocked')
        child = _mk_item(project, 'C835-CC', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        disp = _mk_disposition(project, parent, child, generation=1)
        disp.classification = 'schema_repair'
        disp.save(update_fields=['classification'])

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload['census_valid'])
        self.assertEqual(payload['eligible_keys'], [])
        classes = {a.get('authority_class') for a in payload['ambiguity_rows']}
        self.assertIn('instrument_contradiction', classes)

    def test_parent_unblocked_with_live_link_fail_closed(self):
        """rui P2: non-terminal parent not blocked + live blocks link → invalid."""
        project = _mk_project()
        parent = _mk_item(project, 'C835-UP', 'in_progress')  # not blocked
        child = _mk_item(project, 'C835-UC', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, parent, child, generation=1)

        # Clean sibling that would otherwise freeze
        parent2 = _mk_item(project, 'C835-UP2', 'blocked')
        child2 = _mk_item(project, 'C835-UC2', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child2, target_item=parent2, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, parent2, child2, generation=1)

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload['census_valid'])
        self.assertEqual(payload['eligible_keys'], [])
        classes = {a.get('authority_class') for a in payload['ambiguity_rows']}
        self.assertIn('parent_link_graph_contradiction', classes)
        by_key = {r['child_key']: r for r in payload['rows']}
        self.assertIn('C835-UC', by_key)
        self.assertFalse(by_key['C835-UC']['eligible'])

    def test_terminal_parent_with_live_link_is_benign_skip(self):
        """Terminal parent + live link is exhausted relationship — not invalid."""
        project = _mk_project()
        parent = _mk_item(project, 'C835-TP', 'done')
        child = _mk_item(project, 'C835-TC', 'todo', tags='PLAT-4215')
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        _mk_disposition(project, parent, child, generation=1)

        buf = io.StringIO()
        # May exit 0 if no other ambiguity; or 2 if tag-side orphans — either way
        # the terminal-parent pair must not appear as parent_link_graph_contradiction.
        try:
            with redirect_stdout(buf):
                call_command('census_pls835_waiting_external', format='json')
            code = 0
        except SystemExit as e:
            code = e.code
            pass
        payload = json.loads(buf.getvalue())
        classes = {
            (a.get('authority_class'), a.get('child_key'))
            for a in payload['ambiguity_rows']
        }
        self.assertNotIn(('parent_link_graph_contradiction', 'C835-TC'), classes)
        by_key = {r['child_key']: r for r in payload['rows']}
        self.assertNotIn('C835-TC', by_key)
        del code


class Pls835B2SimpleTypedAdmissionTests(TestCase):
    """Seam B2: fresh typed admit from simple must park without workflow mutation.

    A pass means Shape-1 inventory concerns were noise — in-place typed
    re-admission covers MAIL-85-class simple specimens (wiki §2.3 / §3 B2).
    """

    def test_admit_human_escalation_from_simple_enters_waiting_external(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-B2P', 'blocked')
        child = _mk_item(
            project, 'C835-B2C', 'todo', tags='PLAT-4215',
        )
        # _mk_item already sets workflow_name='simple'
        self.assertEqual(child.workflow_name, 'simple')
        ItemLink.objects.create(
            source_item=child, target_item=parent, link_type='blocks',
            created_by_id=0, created_by_email='kumo@shizuha.com',
        )
        disp = _mk_disposition(project, parent, child, generation=1)

        auth = admit_human_escalation(child, disp)
        self.assertIsNotNone(auth)
        child.refresh_from_db()
        self.assertEqual(child.status, 'todo')
        self.assertNotEqual(child.status, 'waiting_external')
        self.assertEqual(child.workflow_name, 'simple')  # no migration
        self.assertEqual(auth.branch, ExternalWaitAuthority.BRANCH_HUMAN)
        self.assertIn(
            auth.state,
            {
                ExternalWaitAuthority.STATE_ENTER_ARMED,
                ExternalWaitAuthority.STATE_WAITING,
            },
        )
        ev = auth.evidence if isinstance(auth.evidence, dict) else {}
        self.assertEqual(str(ev.get('disposition_id') or ''), str(disp.id))

    def test_admit_refuses_without_blocks_link(self):
        project = _mk_project()
        parent = _mk_item(project, 'C835-B2P2', 'blocked')
        child = _mk_item(project, 'C835-B2C2', 'todo', tags='PLAT-4215')
        disp = _mk_disposition(project, parent, child, generation=1)
        # no ItemLink
        auth = admit_human_escalation(child, disp)
        self.assertIsNone(auth)
        child.refresh_from_db()
        self.assertNotEqual(child.status, 'waiting_external')
        self.assertEqual(child.workflow_name, 'simple')
