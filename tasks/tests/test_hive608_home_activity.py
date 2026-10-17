"""HIVE-608: Pulse HomeActivityEventV1 producer (HLD 1f271d2c).

Covers the acceptance/security contract from the ticket:
- event emitted only after the source row is committed (transaction-after-commit);
- missing/unknown org drops (fail closed) — never published;
- no secret / raw long comment leakage (summary is a capped teaser);
- per-org Redis key isolation;
- a comment / assignment appears in the org's Home stream.
"""
import json
from types import SimpleNamespace

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services import home_activity as ha


class FakeRedis:
    """Minimal xadd-recording stand-in for a Redis Streams client."""

    def __init__(self):
        self.streams = {}  # key -> list[dict]
        self.calls = []    # (key, maxlen, approximate)
        self._entry_times = {}  # (key, entry_index) -> timestamp_ms

    def xadd(self, key, fields, maxlen=None, approximate=True):
        idx = len(self.streams.get(key, []))
        self.streams.setdefault(key, []).append(fields)
        self.calls.append((key, maxlen, approximate))
        # Record the current time so xtrim can compare against minid.
        self._entry_times[(key, idx)] = __import__('time').time() * 1000
        return f'{len(self.calls)}-0'

    def xtrim(self, key, minid=None, approximate=True):
        self.calls.append(('xtrim', key, minid, approximate))
        if minid is None:
            return 0
        minid = int(minid)
        entries = self.streams.get(key, [])
        before = len(entries)
        # Remove entries whose recorded timestamp is older than minid.
        kept = []
        for i, entry in enumerate(entries):
            ts = self._entry_times.get((key, i), 0)
            if ts >= minid:
                kept.append(entry)
        self.streams[key] = kept
        return before - len(kept)

    def events(self, org_id):
        key = ha.STREAM_KEY_FMT.format(org_id=org_id)
        return [json.loads(f['event']) for f in self.streams.get(key, [])]

    def set_entry_time(self, key, index, timestamp_ms):
        """Override the recorded timestamp for an entry (for retention tests)."""
        self._entry_times[(key, index)] = timestamp_ms


def _item_stub(*, org_id, key='HIVE-1', title='A task', priority='high'):
    project = SimpleNamespace(org_id=org_id, key='HIVE')
    return SimpleNamespace(project=project, item_key=key, title=title, priority=priority)


class BuildEventContractTests(TestCase):
    """Pure-function security contract — no Redis, no DB."""

    def test_fail_closed_when_org_missing_or_invalid(self):
        for bad in (None, 0, '', 'abc', -1):
            item = _item_stub(org_id=bad)
            self.assertIsNone(
                ha.build_event(event_type='task.transition', item=item,
                               actor={'kind': 'agent', 'username': 'sora'},
                               summary='x'),
                f'org={bad!r} must fail closed',
            )

    def test_valid_type_required(self):
        item = _item_stub(org_id=7)
        self.assertIsNone(ha.build_event(event_type='task.bogus', item=item,
                                         actor={}, summary='x'))

    def test_summary_and_title_are_capped_no_long_body_leak(self):
        secret_body = 'BEGIN SECRET ' + ('x' * 5000) + ' END SECRET'
        item = _item_stub(org_id=7, title='T' * 500)
        ev = ha.build_event(event_type='task.comment', item=item,
                            actor={'kind': 'user', 'username': 'nao'},
                            summary=secret_body)
        self.assertLessEqual(len(ev['summary']), ha.SUMMARY_MAX)
        self.assertTrue(ev['summary'].endswith('…'))
        self.assertNotIn('END SECRET', ev['summary'])       # tail truncated away
        self.assertLessEqual(len(ev['target']['title']), ha.TITLE_MAX)

    def test_event_shape_and_org_and_href(self):
        item = _item_stub(org_id=7, key='HIVE-42')
        ev = ha.build_event(event_type='task.transition', item=item,
                            actor={'kind': 'agent', 'username': 'sora', 'role': 'Architect'},
                            summary='Sora moved HIVE-42', priority='urgent',
                            metadata={'from_status': 'open', 'to_status': 'in_review'})
        self.assertEqual(ev['version'], 1)
        self.assertEqual(ev['source'], 'pulse')
        self.assertEqual(ev['org_id'], 7)
        self.assertEqual(ev['type'], 'task.transition')
        self.assertEqual(ev['priority'], 'urgent')
        self.assertEqual(ev['target']['href'], '/pulse/browse/HIVE-42')
        self.assertEqual(ev['metadata']['to_status'], 'in_review')
        self.assertEqual(ev['actor']['kind'], 'agent')


class PublishTests(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        ha.set_redis_client(self.redis)

    def tearDown(self):
        ha.set_redis_client(None)

    def test_publish_uses_per_org_key_and_bounded_maxlen(self):
        ha.publish(ha.build_event(event_type='task.comment', item=_item_stub(org_id=7),
                                  actor={}, summary='s'))
        ha.publish(ha.build_event(event_type='task.comment', item=_item_stub(org_id=9),
                                  actor={}, summary='s'))
        self.assertIn('home:activity:v1:org:7', self.redis.streams)
        self.assertIn('home:activity:v1:org:9', self.redis.streams)
        self.assertNotEqual('home:activity:v1:org:7', 'home:activity:v1:org:9')
        # bounded retention requested
        self.assertEqual(self.redis.calls[0][1], ha.STREAM_MAXLEN)

    def test_publish_noop_on_dropped_event(self):
        self.assertFalse(ha.publish(None))
        self.assertFalse(ha.publish(ha.build_event(
            event_type='task.comment', item=_item_stub(org_id=0), actor={}, summary='s')))
        self.assertEqual(self.redis.streams, {})


class WritePathEmitTests(TestCase):
    """DB-backed: real Comment/assignment writes land in the org stream, only
    after the transaction commits."""

    def setUp(self):
        self.redis = FakeRedis()
        ha.set_redis_client(self.redis)
        self.project = Project.objects.create(name='Hive', key='HIVE', org_id=7, access_level='open')
        TeamRoster.objects.create(
            organization_id=7,
            organization_slug='hive-test',
            team_name='engineering',
            tiers=[['kai@shizuha.com']],
        )
        self.item = Item.objects.create(
            mode='task', title='Wire the producer', project=self.project,
            item_key='HIVE-608', priority='high', workflow_name='simple',
            assignment_group='engineering',
        )

    def tearDown(self):
        ha.set_redis_client(None)

    def test_comment_create_emits_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            Comment.objects.create(
                item=self.item, author_id=1, author_email='sora@agents.shizuha.io',
                author_name='Sora', content='Looks good, shipping.',
            )
        events = self.redis.events(7)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'task.comment')
        self.assertEqual(events[0]['org_id'], 7)
        self.assertEqual(events[0]['target']['key'], 'HIVE-608')
        self.assertEqual(events[0]['actor']['kind'], 'agent')  # @agents. → agent

    def test_comment_not_emitted_until_commit(self):
        with self.captureOnCommitCallbacks(execute=False):
            Comment.objects.create(item=self.item, author_id=1,
                                   author_email='nao@shizuha.com', content='wip')
            # on_commit callbacks captured but NOT executed yet → nothing published
            self.assertEqual(self.redis.events(7), [])

    def test_assignment_change_emits(self):
        self.item._activity_actor = (9, 'sora@shizuha.com')
        with self.captureOnCommitCallbacks(execute=True):
            self.item.assign({'id': 2, 'email': 'kai@agents.shizuha.io'})
        events = self.redis.events(7)
        types = [e['type'] for e in events]
        self.assertIn('task.assignment', types)

    def test_assignment_emits_from_save_not_from_assign_save_false(self):
        # Review fix: assign(save=False) must NOT publish (the row isn't saved
        # yet — outside an outer transaction on_commit runs immediately, which
        # would ghost-publish before the source row exists). The event fires from
        # the post-save assignee diff instead — exactly once, after the real save.
        self.item._activity_actor = (9, 'sora@shizuha.com')
        with self.captureOnCommitCallbacks(execute=True):
            self.item.assign({'id': 2, 'email': 'kai@agents.shizuha.io'}, save=False)
        self.assertEqual(self.redis.events(7), [])  # nothing published from assign alone

        with self.captureOnCommitCallbacks(execute=True):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        events = [e for e in self.redis.events(7) if e['type'] == 'task.assignment']
        self.assertEqual(len(events), 1)

    def test_metadata_pr_attach_emits_pr_link(self):
        # HIVE-608 review fix: canonical PR attach is via metadata.pull_requests[]
        # (pulse_link_pr / UI), diffed by _track_pull_request_changes → pr_attached.
        with self.captureOnCommitCallbacks(execute=True):
            md = dict(self.item.metadata or {})
            md['pull_requests'] = [{'url': 'https://github.com/o/r/pull/7', 'number': 7, 'status': 'open'}]
            self.item.metadata = md
            # Real callers (pulse_link_pr / UI) set the actor before save so the
            # activity tracker runs (it early-returns without actor context).
            self.item._activity_actor = (5, 'nao@shizuha.com')
            self.item.save(update_fields=['metadata', 'updated_at'])
        events = self.redis.events(7)
        prlinks = [e for e in events if e['type'] == 'task.pr_link']
        self.assertEqual(len(prlinks), 1)
        self.assertEqual(prlinks[0]['org_id'], 7)
        self.assertEqual(prlinks[0]['metadata'].get('target_key'), '7')

    def test_comment_secret_near_start_is_redacted_not_published(self):
        # HIVE-608 review fix (rei): truncation alone is not a redaction control.
        # A token in the FIRST chars of a comment body survives the summary cap,
        # so it must be redacted BEFORE capping. Put a real GitHub PAT at the very
        # start of the content and assert it never reaches the emitted Home event.
        token = 'ghp_' + ('A' * 36)
        with self.captureOnCommitCallbacks(execute=True):
            Comment.objects.create(
                item=self.item, author_id=1, author_email='sora@agents.shizuha.io',
                author_name='Sora', content=f'{token} please rotate this now',
            )
        events = self.redis.events(7)
        self.assertEqual(len(events), 1)
        raw = json.dumps(events[0])
        self.assertNotIn(token, raw)                       # token nowhere in the event
        self.assertIn('[REDACTED:github_classic_pat]', events[0]['summary'])
        self.assertEqual(events[0]['redaction'], 'suppressed')

    def test_metadata_non_string_redaction_detection(self):
        """Non-string metadata values (lists, ints) with secrets also set redaction='suppressed'."""
        token = 'ghp_' + ('C' * 36)
        event = ha.build_event(
            event_type='task.comment', item=_item_stub(org_id=7),
            actor={}, summary='clean',
            metadata={'tags': [token, 'safe'], 'count': 42},
        )
        self.assertIsNotNone(event)
        self.assertEqual(event['redaction'], 'suppressed')
        raw = json.dumps(event)
        self.assertNotIn(token, raw)

    def test_actor_redaction_detection(self):
        """Actor fields with secrets also set redaction='suppressed'."""
        token = 'ghp_' + ('B' * 36)
        event = ha.build_event(
            event_type='task.comment', item=_item_stub(org_id=7),
            actor={'kind': 'user', 'username': token, 'display_name': 'Bot', 'role': ''},
            summary='clean summary',
        )
        self.assertIsNotNone(event)
        self.assertEqual(event['redaction'], 'suppressed')
        # The actor's username was redacted in the output
        raw = json.dumps(event)
        self.assertNotIn(token, raw)
        self.assertIn('[REDACTED:github_classic_pat]', event['actor']['username'])

    def test_publish_trims_24h_age_bound(self):
        """HLD §4: publish also trims entries older than 24h via XTRIM MINID."""
        ha.publish(ha.build_event(event_type='task.comment', item=_item_stub(org_id=7),
                                  actor={}, summary='s'))
        # After xadd, publish calls xtrim with minid ~ (now_ms - 24h)
        xtrim_calls = [c for c in self.redis.calls if c[0] == 'xtrim']
        self.assertEqual(len(xtrim_calls), 1)
        key, minid, approx = xtrim_calls[0][1], xtrim_calls[0][2], xtrim_calls[0][3]
        self.assertEqual(key, 'home:activity:v1:org:7')
        self.assertFalse(approx)  # exact MINID, not approximate
        # minid should be roughly now_ms - 86_400_000 (within a few seconds)
        now_ms = __import__('time').time() * 1000
        expected_minid = int(now_ms - ha.STREAM_24H_MS)
        actual_minid = int(minid)
        self.assertAlmostEqual(actual_minid, expected_minid, delta=5_000)

    def test_xtrim_evicts_old_entries(self):
        """Semantic: XTRIM MINID actually evicts entries older than 24h."""
        key = ha.STREAM_KEY_FMT.format(org_id=7)
        now_ms = __import__('time').time() * 1000
        # Add a "current" entry.
        ha.publish(ha.build_event(event_type='task.comment', item=_item_stub(org_id=7),
                                  actor={}, summary='current'))
        self.assertEqual(len(self.redis.events(7)), 1)
        # Manually add an old entry (25h ago) directly to the stream.
        old_ts = now_ms - ha.STREAM_24H_MS - 3600_000  # 25h ago
        self.redis.streams.setdefault(key, []).append({'event': '{"old":true}'})
        old_idx = len(self.redis.streams[key]) - 1
        self.redis.set_entry_time(key, old_idx, old_ts)
        self.assertEqual(len(self.redis.events(7)), 2)  # both present
        # Now publish again — the xtrim should evict the old entry.
        ha.publish(ha.build_event(event_type='task.comment', item=_item_stub(org_id=7),
                                  actor={}, summary='newer'))
        events = self.redis.events(7)
        self.assertEqual(len(events), 2)  # current + newer; old evicted
        self.assertNotIn('old', str(events))

    def test_transition_helper_emits_for_org(self):
        with self.captureOnCommitCallbacks(execute=True):
            ha.emit_transition(self.item, user=None, from_status='open', to_status='in_review')
        events = self.redis.events(7)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'task.transition')
        self.assertEqual(events[0]['metadata']['to_status'], 'in_review')
