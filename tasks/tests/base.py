"""
Shared base test classes for Shizuha Pulse tests.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, ItemLink

User = get_user_model()


def satisfy_blocked_invariant(item, blocker=None):
    """Make a blocked-item fixture valid under the PLAT-4215 DB contract.

    Tests about unrelated projections/sweeps may still need a durable blocked row.
    Prefer a caller-supplied semantic blocker; otherwise add a small owned active
    blocker and suppress the link signal because the parent is already blocked.
    """
    if blocker is None:
        from tasks.services.workflow_service import WorkflowService
        existing = next((
            candidate
            for candidate in WorkflowService._active_blockers_for(item)
            if (candidate.mode or '').strip().lower() == 'task'
            and candidate.assignee_id
            and (candidate.assignee_email or '').strip()
        ), None)
        if existing is not None:
            return existing
        blocker_user_id = 900000 + int(item.pk or 0)
        blocker_email = f'blocker-{item.pk}@example.com'
        blocker_group = ''
        if item.project_id:
            from tasks.models import TeamRoster
            blocker_group = 'test-fixtures'
            roster, _ = TeamRoster.objects.get_or_create(
                organization_id=item.project.org_id,
                team_name=blocker_group,
                defaults={
                    'organization_slug': f'test-org-{item.project.org_id}',
                    'tiers': [[blocker_email]],
                },
            )
            tiers = roster.tiers or []
            members = {
                str(member).strip().lower()
                for tier in tiers
                for member in (tier if isinstance(tier, list) else [tier])
            }
            if blocker_email.lower() not in members:
                if not tiers:
                    tiers = [[]]
                tiers[0].append(blocker_email)
                roster.tiers = tiers
                roster.save(update_fields=['tiers', 'updated_at'])
        blocker = Item.objects.create(
            mode='task',
            title=f'Test-owned blocker for {item.item_key or item.pk}',
            status='pending',
            project=item.project,
            workflow_name=item.workflow_name or 'simple',
            assignment_group=blocker_group,
            assignee_id=blocker_user_id,
            assignee_email=blocker_email,
        )
    elif not blocker.assignee_id or not (blocker.assignee_email or '').strip():
        blocker.assignee_id = 900000 + int(blocker.pk or 0)
        blocker.assignee_email = f'blocker-{blocker.pk}@example.com'
        if blocker.project_id:
            from tasks.models import TeamRoster
            blocker.assignment_group = 'test-fixtures'
            roster, _ = TeamRoster.objects.get_or_create(
                organization_id=blocker.project.org_id,
                team_name=blocker.assignment_group,
                defaults={
                    'organization_slug': f'test-org-{blocker.project.org_id}',
                    'tiers': [[blocker.assignee_email]],
                },
            )
            tiers = roster.tiers or []
            members = {
                str(member).strip().lower()
                for tier in tiers
                for member in (tier if isinstance(tier, list) else [tier])
            }
            if blocker.assignee_email.lower() not in members:
                if not tiers:
                    tiers = [[]]
                tiers[0].append(blocker.assignee_email)
                roster.tiers = tiers
                roster.save(update_fields=['tiers', 'updated_at'])
        blocker.save(update_fields=[
            'assignment_group', 'assignee_id', 'assignee_email',
        ])

    if not ItemLink.objects.filter(
        source_item=blocker, target_item=item, link_type='blocks',
    ).exists():
        link = ItemLink(
            source_item=blocker,
            target_item=item,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()
    return blocker


def create_blocked_item(*, blocker=None, pre_block_status='open', **kwargs):
    """Create a valid blocked fixture without bypassing the model entry fence.

    A new parent cannot already have a linked blocker, so PLAT-4215 rejects
    ``Item.objects.create(status='blocked')``. Build the durable graph in the same
    order as production: create the parent in a working state, link an owned task,
    then persist ``blocked``.
    """
    desired_workflow_status = kwargs.pop('workflow_status', None)
    kwargs.pop('status', None)
    item = Item.objects.create(status=pre_block_status, **kwargs)
    satisfy_blocked_invariant(item, blocker=blocker)
    item.status = 'blocked'
    update_fields = ['status']
    if desired_workflow_status is not None:
        item.workflow_status = 'blocked'
        update_fields.append('workflow_status')
    item.save(update_fields=update_fields)
    return item


class AuthenticatedAPITestCase(APITestCase):
    """Base test case with authenticated staff user.

    Uses a staff user so that ItemProjectPermission is bypassed,
    allowing tests to focus on API functionality rather than
    permission logic. Permission-specific tests should use
    non-staff users explicitly.
    """

    def setUp(self):
        """Set up authenticated client with staff user."""
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)
