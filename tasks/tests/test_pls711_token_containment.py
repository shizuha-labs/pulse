"""PLS-711 §7.1 containment: a failed WIP verify must not poison later saves.

@hiro's ruling, fact 5: `_verify_wip_reservation` is called at models.py:1315,
BEFORE the try whose except sits at :1613, so a raise there propagates straight
out and the cleanup at :1633 is unreachable. `_wip_reservation_token` is an
INSTANCE attribute, so it stays attached -- and every later save on that same
in-memory object re-enters the same failing verify.

That is the mechanism that turns one crash into REPEATED destruction of the
escalation payload, which is why this ships ahead of the root-cause fix.

⚠️ THE FIXTURE MUST SPAN >=2 SAVES OF THE SAME INSTANCE (@hiro §8, and the
stateful-recurrence rule in verify-before-push). A single-save fixture is
structurally blind to stickiness -- it cannot observe the property under test,
because the property IS what happens on the second save.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService

User = get_user_model()


class Pls711TokenContainmentTests(TransactionTestCase):

    def setUp(self):
        self.actor = User.objects.create_user(
            username='pls711c', email='pls711c@example.com', is_staff=True)
        # No project: Item.save()'s ADR-0041 org guard returns early when
        # project_id is NULL, and this fixture is about the WIP-token lifetime,
        # not org routing. Attaching one would arm a guard unrelated to the
        # property under test (learned on PLS-722).
        self.item = Item.objects.create(
            item_key='P711C-1', mode='task',
            title='containment', description='x', workflow_name='autonomous-dev',
            status='open', assignee_id=self.actor.id,
            assignee_email=self.actor.email,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )

    def test_a_failed_verify_detaches_the_token_so_the_NEXT_save_is_clean(self):
        """The load-bearing one: two saves, and the second must not re-verify."""
        self.item._wip_reservation_token = 'tok-pls711-containment'
        calls = []

        def boom(item):
            calls.append(getattr(item, '_wip_reservation_token', None))
            raise ValidationError({'assignee_id': 'wip_reservation_target_mismatch'})

        with patch.object(WorkflowService, '_verify_wip_reservation', staticmethod(boom)), \
                patch.object(
                    WorkflowService, '_release_unconsumed_wip_reservation',
                    staticmethod(lambda i: (
                        delattr(i, '_wip_reservation_token')
                        if hasattr(i, '_wip_reservation_token') else False))):
            # SAVE 1 -- raises, as it must; the fix does not swallow it.
            with self.assertRaises(ValidationError):
                self.item.save(update_fields=['title', 'updated_at'])

            # SAVE 2 on the SAME instance. Before the containment the token was
            # still attached here and this re-entered the same failing verify.
            self.item.title = 'second write'
            self.item.save(update_fields=['title', 'updated_at'])

        self.assertEqual(
            len(calls), 1,
            f'verify ran {len(calls)}x across two saves -- the token survived the '
            'failure and poisoned the next save, which is the repeat-destruction '
            'mechanism this containment exists to break.',
        )
        self.assertFalse(
            hasattr(self.item, '_wip_reservation_token'),
            'token still attached after a failed verify',
        )

    def test_CONTROL_the_original_error_survives_a_failing_release(self):
        """Containment must not become suppression -- OR substitution.

        Two ways this could go wrong, and this pins both:

        1. If the raise were swallowed, the two-save test above would STILL pass
           (verify runs once) while the guard silently stopped guarding.
        2. The release itself can raise -- its queryset filters on a UUID
           column, so a malformed token yields "not a valid UUID". That is how
           this test originally failed: cleanup REPLACED the verify's real
           diagnosis, and an operator would have seen a UUID parse error instead
           of wip_reservation_target_mismatch. The token here is deliberately
           NOT a UUID so that path stays exercised.
        """
        self.item._wip_reservation_token = 'tok-pls711-control'

        def boom(item):
            raise ValidationError({'assignee_id': 'wip_reservation_target_mismatch'})

        with patch.object(WorkflowService, '_verify_wip_reservation', staticmethod(boom)):
            with self.assertRaises(ValidationError) as caught:
                self.item.save(update_fields=['title', 'updated_at'])

        self.assertIn('wip_reservation_target_mismatch', str(caught.exception))

    def test_CONTROL_a_successful_verify_is_untouched(self):
        """The happy path must not change: no release, reservation consumed."""
        self.item._wip_reservation_token = 'tok-pls711-ok'
        released = []

        with patch.object(WorkflowService, '_verify_wip_reservation',
                          staticmethod(lambda item: None)), \
                patch.object(WorkflowService, '_release_unconsumed_wip_reservation',
                             staticmethod(lambda i: released.append(1))):
            self.item.save(update_fields=['title', 'updated_at'])

        self.assertEqual(released, [], 'release fired on a SUCCESSFUL verify')
