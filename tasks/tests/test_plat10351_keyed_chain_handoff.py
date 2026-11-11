"""PLAT-10351: assignment transfer carries keyed-chain state, or says it is absent."""
import unittest

from tasks.services.keyed_chain_handoff import chain_snapshot


class _Item:
    def __init__(self, status, metadata):
        self.status = status
        self.metadata = metadata


class ChainSnapshotTests(unittest.TestCase):
    def test_reads_recorded_pointer(self):
        item = _Item('in_progress', {
            'keyed_chain_position': 'receipt minted, close not fired',
            'trail_pointer': {'content_address': 'abc123'},
            'next_action': 'fire close with the minted receipt',
        })
        self.assertEqual(
            chain_snapshot(item),
            ('receipt minted, close not fired', 'abc123', 'fire close with the minted receipt'),
        )

    def test_missing_hash_stays_explicit(self):
        item = _Item('in_review', {})
        position, receipt, next_action = chain_snapshot(item)
        self.assertEqual(position, 'in_review')
        self.assertEqual(receipt, 'none')
        self.assertIn('do not re-derive', next_action)


if __name__ == '__main__':
    unittest.main()
