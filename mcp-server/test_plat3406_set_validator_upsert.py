"""
Regression tests for PLAT-3406: typed `set_validator` must upsert the named
validator while PRESERVING every other validator on the transition.

The #14196 failure class: applying `set_validator
customer_facing_readiness_signoff` to `Approve & Schedule` replaced the whole
validators list and silently dropped the pre-existing `min_seconds_in_review`
gate.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

from typed_change_spec import apply_operation


def _detail(validators):
    return {
        'id': 7,
        'name': 'change-request',
        'statuses': [],
        'transitions': [{
            'id': '2f7b28d2-bfb4-43d3-ba65-4ea3afa65178',
            'name': 'Approve & Schedule',
            'validators': validators,
        }],
    }


class SetValidatorUpsertTests(unittest.TestCase):
    def _apply(self, validators, op_validator='customer_facing_readiness_signoff',
               op_value=None):
        patches = []

        def api(method, path, data=None):
            patches.append((method, path, data))
            return {'ok': True}

        op = {
            'op': 'set_validator',
            'workflow': 'change-request',
            'transition': 'Approve & Schedule',
            'validator': op_validator,
            'value': op_value if op_value is not None else {'type': op_validator},
        }
        ok, msg = apply_operation(op, api, lambda ref: _detail(validators))
        return ok, msg, patches

    def test_existing_validator_is_preserved(self):
        # The exact #14196 shape: min_seconds_in_review already on the
        # transition, a new signoff validator applied on top.
        ok, msg, patches = self._apply(
            [{'type': 'min_seconds_in_review', 'seconds': 600}])

        self.assertTrue(ok, msg)
        (method, path, data), = patches
        self.assertEqual(method, 'PATCH')
        sent = data['validators']
        types = [v['type'] for v in sent]
        self.assertIn('min_seconds_in_review', types)
        self.assertIn('customer_facing_readiness_signoff', types)
        # The preserved validator keeps its original payload untouched.
        self.assertIn({'type': 'min_seconds_in_review', 'seconds': 600}, sent)
        self.assertIn('1 other validator(s) preserved', msg)

    def test_same_named_validator_is_updated_not_duplicated(self):
        ok, msg, patches = self._apply(
            [{'type': 'min_seconds_in_review', 'seconds': 600},
             {'type': 'customer_facing_readiness_signoff', 'value': {'old': True}}])

        self.assertTrue(ok, msg)
        sent = patches[0][2]['validators']
        signoffs = [v for v in sent if v['type'] == 'customer_facing_readiness_signoff']
        self.assertEqual(len(signoffs), 1)
        self.assertNotIn({'type': 'customer_facing_readiness_signoff',
                          'value': {'old': True}}, sent)
        self.assertIn({'type': 'min_seconds_in_review', 'seconds': 600}, sent)
        self.assertIn("updated existing validator", msg)

    def test_rerun_is_idempotent(self):
        # PLAT-253 #5: the apply loop re-executes earlier succeeded ops on
        # re-run — a second identical apply must not grow the list.
        first_ok, _, first = self._apply([{'type': 'min_seconds_in_review', 'seconds': 600}])
        self.assertTrue(first_ok)
        after_first = first[0][2]['validators']

        second_ok, _, second = self._apply(after_first)
        self.assertTrue(second_ok)
        self.assertEqual(len(second[0][2]['validators']), len(after_first))

    def test_empty_validator_list_gets_single_entry(self):
        ok, msg, patches = self._apply([])
        self.assertTrue(ok, msg)
        sent = patches[0][2]['validators']
        self.assertEqual([v['type'] for v in sent],
                         ['customer_facing_readiness_signoff'])
        self.assertIn("added validator", msg)

    def test_string_stored_validators_are_parsed_not_chargrown(self):
        # PLAT-519 defense: a prior bad write stored the list as a JSON string.
        ok, msg, patches = self._apply(
            '[{"type": "min_seconds_in_review", "seconds": 600}]')
        self.assertTrue(ok, msg)
        sent = patches[0][2]['validators']
        self.assertEqual(len(sent), 2)
        self.assertIn({'type': 'min_seconds_in_review', 'seconds': 600}, sent)

    def test_unknown_transition_still_errors(self):
        def api(method, path, data=None):
            return {'ok': True}

        op = {'op': 'set_validator', 'workflow': 'change-request',
              'transition': 'No Such Transition', 'validator': 'x', 'value': {}}
        ok, msg = apply_operation(op, api, lambda ref: _detail([]))
        self.assertFalse(ok)
        self.assertIn('not found', msg)


if __name__ == '__main__':
    unittest.main()
