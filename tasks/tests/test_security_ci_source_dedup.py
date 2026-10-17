"""Active security-ci source_id is a unique rolling identity."""
from django.db import IntegrityError
from django.test import TestCase

from tasks.models import Item, Project


class SecurityCiSourceIdUniqTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='PLAT', org_id=1, name='Platform')

    def test_second_active_row_with_same_source_id_is_rejected(self):
        Item.objects.create(
            project=self.project,
            item_key='PLAT-KEEP',
            title='[security-ci] Findings ledger — shizuha-labs/cortex',
            source='security-ci',
            source_id='security-ci:shizuha-labs/cortex:ledger',
            status='found',
            workflow_name='security-finding',
        )
        with self.assertRaises(IntegrityError):
            Item.objects.create(
                project=self.project,
                item_key='PLAT-DUP',
                title='[security-ci] Findings ledger — shizuha-labs/cortex',
                source='security-ci',
                source_id='security-ci:shizuha-labs/cortex:ledger',
                status='found',
                workflow_name='security-finding',
            )

    def test_terminal_row_does_not_block_a_new_active_item(self):
        Item.objects.create(
            project=self.project,
            item_key='PLAT-OLD',
            title='[security-ci] Findings ledger — shizuha-labs/cortex',
            source='security-ci',
            source_id='security-ci:shizuha-labs/cortex:ledger',
            status='cancelled',
            workflow_name='security-finding',
        )
        Item.objects.create(
            project=self.project,
            item_key='PLAT-NEW',
            title='[security-ci] Findings ledger — shizuha-labs/cortex',
            source='security-ci',
            source_id='security-ci:shizuha-labs/cortex:ledger',
            status='found',
            workflow_name='security-finding',
        )
        self.assertEqual(
            Item.objects.filter(
                source_id='security-ci:shizuha-labs/cortex:ledger',
                status='found',
            ).count(),
            1,
        )
