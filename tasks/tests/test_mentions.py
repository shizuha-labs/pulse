"""PLS-100 / PLS-735: @-mention parser + relay body contract."""
from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase

from tasks.services.mentions import _mention_message, extract_mentions


class ExtractMentionsTest(SimpleTestCase):
    def test_single_mention(self):
        self.assertEqual(extract_mentions("Hey @hana please look"), ["hana"])

    def test_multiple_mentions_order_preserved(self):
        self.assertEqual(extract_mentions("@hana and @kei"), ["hana", "kei"])

    def test_dedup_and_lowercase(self):
        self.assertEqual(extract_mentions("@Hana @hana dup"), ["hana"])

    def test_trailing_dot_stripped(self):
        self.assertEqual(extract_mentions("ping @hana."), ["hana"])

    def test_dotted_handle_preserved(self):
        self.assertEqual(extract_mentions("cc @first.last"), ["first.last"])

    def test_email_is_not_a_mention(self):
        # negative lookbehind: @ preceded by a handle char (the 'a') is not a mention
        self.assertEqual(extract_mentions("email me at a@b.com please"), [])

    def test_mention_after_punctuation(self):
        self.assertEqual(extract_mentions("(@hana) and @kei!"), ["hana", "kei"])

    def test_empty_and_none(self):
        self.assertEqual(extract_mentions(""), [])
        self.assertEqual(extract_mentions(None), [])

    def test_no_mentions(self):
        self.assertEqual(extract_mentions("no mentions here"), [])


class MentionRelayMessageTest(SimpleTestCase):
    """PLS-735 AC-3 / AC-4a / AC-4b — relay body contract."""

    def test_instructs_list_comments_not_get_task(self):
        body = _mention_message(
            'banto',
            'PLS-711',
            'Carrier',
            '## @jun hello',
            comment_id=42,
            posted_at=datetime(2026, 8, 3, 11, 10, 55, tzinfo=dt_timezone.utc),
        )
        self.assertIn('pulse_list_comments("PLS-711")', body)
        self.assertIn('pulse_get_task returns no comments', body)
        # Must not be the sole instruction (old bug: 98.3% of relays).
        self.assertNotIn('Use pulse_get_task("PLS-711") for full context.', body)

    def test_carries_comment_id_and_posted_timestamp(self):
        body = _mention_message(
            'reika',
            'PLS-735',
            'Mention lag',
            'excerpt body',
            comment_id=99,
            posted_at=datetime(2026, 8, 3, 6, 8, 44, tzinfo=dt_timezone.utc),
        )
        self.assertIn('comment_id: 99', body)
        self.assertIn('posted: 2026-08-03T06:08:44Z', body)
        self.assertIn('several pages deep', body)

    def test_works_without_optional_metadata(self):
        body = _mention_message('x', 'T-1', 't', 'e')
        self.assertIn('pulse_list_comments("T-1")', body)
        self.assertNotIn('comment_id:', body)
        self.assertNotIn('posted:', body)
