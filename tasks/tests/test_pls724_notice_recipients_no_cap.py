"""
PLS-724 regression — notification-recipient lookup must not enforce WIP cap.

`_notice_recipients` decides WHO should be told about an escalation outcome.
It is a read-only lookup with respect to capacity — nothing is being assigned.
The old call omitted `enforce_cap`, so it used the default True, which (a)
reserved a WIP slot against the escalated parent item and (b) dropped the
recipient from the outbox whenever the coordinator team was at its WIP cap (a
full team silently stopped receiving escalation notices).

Fix: pass `enforce_cap=False` so a recipient lookup never touches capacity.
These tests pin that contract: the lookup must pass enforce_cap=False, and a
capped coordinator team must still yield a recipient.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.escalation_routing import _Plan, _notice_recipients

User = get_user_model()


class PLS724NoticeRecipientsNoCapTests(TestCase):
    def setUp(self):
        self.coordinator = User.objects.create_user(
            username='coord', email='coord@shizuha.com', password='x', is_staff=False,
        )
        self.project = Project.objects.create(
            name='P', key='P', org_id=1, access_level='open', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['coord@shizuha.com']],
                'is_active': True,
            },
        )
        self.item = Item.objects.create(
            mode='task', title='escalated', project=self.project,
            assignee_id=self.coordinator.id, assignee_email='coord@shizuha.com',
            status='in_progress', workflow_name='simple',
        )
        # reason_code routes the coordinator group to "engineering".
        self.plan = _Plan(
            classification='typed', outcome='routed',
            reason_code='postcondition_verify', target_status='blocked',
            target_group='engineering',
        )

    def test_recipient_lookup_passes_enforce_cap_false(self):
        """_notice_recipients must resolve the recipient WITHOUT cap enforcement."""
        captured = {}

        def _fake_resolve(team_name, **kwargs):
            captured['enforce_cap'] = kwargs.get('enforce_cap')
            return self.coordinator.id, 'coord@shizuha.com'

        with patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            side_effect=_fake_resolve,
        ):
            recipients = _notice_recipients(self.item, self.plan)

        self.assertIs(captured.get('enforce_cap'), False)
        self.assertTrue(
            any(r['user_id'] == self.coordinator.id for r in recipients),
            'capped coordinator team must still yield a recipient',
        )

    def test_recipient_lookup_leaves_no_wip_reservation_state(self):
        """A read-only lookup must not stamp reservation state on the item."""
        with patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(self.coordinator.id, 'coord@shizuha.com'),
        ):
            _notice_recipients(self.item, self.plan)
        # The cap-enforcing path stamps a transient _wip_reservation_token; a
        # recipient lookup must never leave one on the item.
        self.assertFalse(hasattr(self.item, '_wip_reservation_token'))
