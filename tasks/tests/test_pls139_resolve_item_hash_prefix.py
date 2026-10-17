"""PLS-139: _resolve_item must tolerate a leading '#'.

pulse_get_my_tasks renders numeric-id alert tasks as '#6507'; agents pass
that string straight back into transition/lookup calls. Before the fix,
'#6507' matched neither the item_key branch nor int(), so the item was
reported not-found and the available-transitions endpoint stranded it at
status 'unknown'.
"""
from django.test import TestCase

from tasks.models import Item, Project
from tasks.views import _resolve_item


class ResolveItemHashPrefixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(name='Resolve Hash', key='PLS139', org_id=1)
        cls.item = Item.objects.create(
            mode='task',
            title='resolve-by-hash task',
            project=cls.project,
            item_key='PLS139-1',
            workflow_name='simple',
        )

    def test_plain_numeric_id_resolves(self):
        self.assertEqual(_resolve_item(str(self.item.pk)).pk, self.item.pk)

    def test_hash_prefixed_numeric_id_resolves(self):
        # The bug: '#<pk>' previously raised Item.DoesNotExist.
        self.assertEqual(_resolve_item(f'#{self.item.pk}').pk, self.item.pk)

    def test_hash_prefixed_with_surrounding_whitespace_resolves(self):
        self.assertEqual(_resolve_item(f'  #{self.item.pk}  ').pk, self.item.pk)

    def test_item_key_still_resolves(self):
        self.assertEqual(_resolve_item('PLS139-1').pk, self.item.pk)

    def test_unknown_hash_id_still_raises(self):
        with self.assertRaises(Item.DoesNotExist):
            _resolve_item('#99999999')
