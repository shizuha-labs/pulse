"""Canonical Team identity and organization-qualified lead resolution."""

from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.serializers import WorkflowTransitionDetailSerializer
from tasks.services.workflow_service import WorkflowService


class BusinessTeamAliasTests(TestCase):
    def test_legacy_org_prefixed_names_persist_as_canonical_team(self):
        roster = TeamRoster.objects.create(
            organization_slug='personal-3',
            team_name='personal-accounting',
            tiers=[['sato@shizuha.com']],
        )

        roster.refresh_from_db()
        self.assertEqual(roster.team_name, 'accounting')
        self.assertEqual(
            WorkflowService._canonical_team_name('digital-accounting'),
            'accounting',
        )
        self.assertEqual(
            WorkflowService._canonical_team_name('securities-engineering'),
            'engineering',
        )
        self.assertEqual(
            WorkflowService._canonical_team_name('platform-ops'),
            'devops',
        )
        self.assertEqual(
            TeamRoster._CANONICAL_ALIASES['platform-ops'],
            'devops',
        )

    def test_item_insert_canonicalizes_legacy_business_name(self):
        project = Project.objects.create(
            name='Books Alias Insert', key='BKAI', org_id=4,
        )

        item = Item.objects.create(
            project=project, item_key='BKAI-1', mode='task',
            title='Canonical team insert',
            assignment_group='personal-accounting',
        )

        self.assertEqual(item.assignment_group, 'accounting')

    def test_item_insert_canonicalizes_legacy_platform_ops_name(self):
        project = Project.objects.create(
            name='Platform Alias Insert', key='PLATAI', org_id=1,
        )

        item = Item.objects.create(
            project=project, item_key='PLATAI-1', mode='task',
            title='Canonical DevOps team insert',
            assignment_group='platform-ops',
        )

        self.assertEqual(item.assignment_group, 'devops')

    def test_transition_configuration_canonicalizes_team_alias(self):
        serializer = WorkflowTransitionDetailSerializer()

        data = serializer.validate({
            'name': 'Legacy route',
            'to_status': 'open',
            'post_functions': [
                {'type': 'assign_team', 'value': 'platform-ops'},
            ],
        })

        self.assertEqual(
            data['post_functions'][0]['value'],
            'devops',
        )

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=lambda org_id: {
            1: {'id': 1, 'slug': 'shizuha'},
            4: {'id': 4, 'slug': 'personal-3'},
        }.get(int(org_id)),
    )
    def test_same_named_team_lead_is_resolved_from_task_organization(self, _):
        TeamRoster.objects.create(
            organization_slug='shizuha',
            team_name='accounting',
            tiers=[['global-accountant@shizuha.com']],
        )
        TeamRoster.objects.create(
            organization_slug='personal-3',
            team_name='accounting',
            tiers=[['personal-accountant@shizuha.com']],
        )
        project = Project.objects.create(
            name='Books', key='BKALIAS', org_id=4,
        )
        item = Item.objects.create(
            project=project, item_key='BKALIAS-1', mode='task',
            title='Prepare return', assignment_group='accounting',
        )

        self.assertEqual(
            WorkflowService._resolve_team_lead_email('accounting', item=item),
            'personal-accountant@shizuha.com',
        )
        self.assertEqual(
            WorkflowService._resolve_team_lead_email(
                'accounting', organization_slug='shizuha',
            ),
            'global-accountant@shizuha.com',
        )
