"""PLAT-1041: sweep_active_blocker_links must not process terminal-status items.

PR #212 adds Q exclusions for both link directions when the blocked item is
terminal (done, completed, cancelled, etc.).  Regression tests:
  1. Terminal blocked item — sweep skips it; auto_block_from_link is never called.
  2. Non-terminal blocked item — sweep still processes it (guard is not over-broad).
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, ItemLink, Project


User = get_user_model()


def _make_project():
    return Project.objects.create(name='PLAT1041', key='P1041', org_id=1)


def _make_user(username):
    email = f'{username}@shizuha.com'
    return User.objects.create_user(username=username, email=email, password='x')


class BlockerSweepTerminalGuardTests(TestCase):
    """Terminal blocked items must be excluded from sweep_active_blocker_links."""

    def _run_sweep(self):
        from tasks.tasks import sweep_active_blocker_links
        # The sweep is a Celery task; call its underlying function directly.
        return sweep_active_blocker_links.apply(args=[], kwargs={'limit': 500}).get()

    def test_terminal_blocked_item_skipped_by_sweep(self):
        """A done item with an active is_blocked_by link must not reach auto_block_from_link."""
        actor = _make_user('p1041_actor')
        project = _make_project()

        blocker = Item.objects.create(
            title='active blocker', project=project, status='in_progress', mode='task',
        )
        done_blocked = Item.objects.create(
            title='already done — must not be re-blocked', project=project,
            status='done', mode='task',
        )
        # done_blocked is_blocked_by blocker
        ItemLink.objects.create(
            source_item=done_blocked,
            target_item=blocker,
            link_type='is_blocked_by',
            created_by_id=actor.id,
            created_by_email=actor.email,
        )

        from tasks.services.workflow_service import WorkflowService
        with mock.patch.object(
            WorkflowService, 'auto_block_from_link', wraps=WorkflowService.auto_block_from_link
        ) as mock_auto_block:
            self._run_sweep()

        # auto_block_from_link must never have been called with the terminal item's link.
        for call in mock_auto_block.call_args_list:
            link = call.args[0] if call.args else call.kwargs.get('link')
            if link is not None:
                self.assertNotEqual(
                    link.source_item_id, done_blocked.id,
                    'sweep called auto_block_from_link on terminal blocked item',
                )

        # The done item must still be done — not re-blocked.
        done_blocked.refresh_from_db()
        self.assertEqual(done_blocked.status, 'done')

    def test_non_terminal_blocked_item_is_still_processed(self):
        """A non-terminal blocked item with an active link must still be picked up by the sweep."""
        actor = _make_user('p1041_actor2')
        project = Project.objects.create(name='PLAT1041b', key='P1041B', org_id=1)

        blocker = Item.objects.create(
            title='active blocker 2', project=project, status='in_progress', mode='task',
        )
        open_blocked = Item.objects.create(
            title='open item — should be processed', project=project,
            status='open', mode='task',
        )
        ItemLink.objects.create(
            source_item=open_blocked,
            target_item=blocker,
            link_type='is_blocked_by',
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        # Link creation now auto-transitions the blocked item to 'blocked' via a
        # signal, which the sweep would then correctly skip. This sweep is the
        # BACKSTOP for the import/script paths that bypass that signal — simulate
        # that by forcing the item back to a non-blocked, non-terminal status
        # (update() bypasses the save() signal) so there is work for the sweep.
        Item.objects.filter(pk=open_blocked.pk).update(status='open')

        from tasks.services.workflow_service import WorkflowService
        with mock.patch.object(
            WorkflowService, 'auto_block_from_link', return_value=True
        ) as mock_auto_block:
            self._run_sweep()

        # auto_block_from_link must have been called at least once for the open item.
        self.assertTrue(
            mock_auto_block.called,
            'sweep did not call auto_block_from_link for a non-terminal blocked item',
        )
