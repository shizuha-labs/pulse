"""PLS-421: direct-review capability must fence availability auto-failover.

Live regression (HIVE-626, reported by sora@shizuha.com): an Architecture-only
exact-version review task was auto-failed over from sora@shizuha.com to an
Engineering seat because availability failover resolved from the item's
in-progress ``assignment_group=engineering`` instead of preserving the direct
assignee's seat constraints (``architecture-review`` label, reviewer
independence). The incompatible substitute then posted an ``approved`` verdict
one minute before the intended reviewer's changes-required verdict.

Fix under test: the failover drain substitutes another resident seat on the
current Architecture roster when one exists (AC1) and holds fail-loud with a
``direct_review_authority`` routability hold when none does (AC2) — it must
never select an Engineering implementer for the review (AC4), while normal
compatible failover keeps working (AC5).
"""
import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_rebalance_pods


# Engineering pod: the down Architecture reviewer is a member (the mis-grouping
# that made the original regression possible) plus a real Engineering seat.
_ENG_ROSTER = {
    'engineering': [['sora@shizuha.com', 'ken@shizuha.com']],
}
_ARCH_ROSTER_FULL = {
    'architecture': [['sora@shizuha.com', 'nova@shizuha.com']],
}
_ARCH_ROSTER_STARVED = {
    'architecture': [['sora@shizuha.com']],
}

_USERS = {
    'sora@shizuha.com': (204, 'sora@shizuha.com'),
    'ken@shizuha.com':  (205, 'ken@shizuha.com'),
    'nova@shizuha.com': (206, 'nova@shizuha.com'),
}


def _fake_user_service():
    class FakeUS:
        def get_user_by_email(self, email):
            rec = _USERS.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def get_user_by_id(self, user_id):
            for uid, email in _USERS.values():
                if int(user_id) == uid:
                    return {'id': uid, 'email': email}
            return None
    return FakeUS()


_ENV = {
    'PULSE_API_HEALTH_URL': 'disabled',
    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
}


class _FakeRedis:
    """Deterministic Redis stub for the sweep.

    The CI pod shares a live Redis with real bridges: real heartbeat stamps,
    real disabled/hibernated sets, real streak maps. Those can silently satisfy
    or swallow the protection gates (shin run 9296: _soft_unavailable_owner_
    protected returned first on a REAL recent heartbeat). The stub makes the
    sweep drive every verdict from the patched fixtures only — no runtime
    disabled/hibernated sets, no heartbeat stamps, empty streak maps.
    """

    def get(self, key):
        return None

    def setex(self, key, ttl, value):
        return True

    def set(self, key, value, **kwargs):
        return True

    def expire(self, key, ttl):
        return True

    def delete(self, key):
        return True

    def incr(self, key):
        return 1

    def scan_iter(self, pattern, **kwargs):
        return []


class DirectReviewFailoverFencingTests(TestCase):
    """HIVE-626 shape: architecture-review + direct Architecture assignee +
    transient unavailability + engineering in-progress group."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Direct-review failover fencing',
            key='P421A',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ENG_ROSTER['engineering'],
                'is_active': True,
            },
        )

    def _review_item(self):
        return Item.objects.create(
            title='HLD v16 / ADR-0031 v8 exact-version architecture review',
            mode='task', status='in_progress', priority='urgent',
            project=self.project,
            assignment_group='engineering',
            labels=['architecture-review'],
            created_by_email='ken@shizuha.com',
            assignee_id=204, assignee_email='sora@shizuha.com',
        )

    @patch.dict(os.environ, _ENV)
    def test_failover_substitutes_architecture_peer(self):
        """AC1: with a resident Architecture peer available, the drain moves
        the review to that peer — never to the Engineering seat."""
        self._review_item()
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ARCH_ROSTER_FULL['architecture'],
                'is_active': True,
            },
        )
        # ken must stay AVAILABLE: with only nova available, sora AND ken both
        # read unavailable, the engineering pod has no live member, and the sweep
        # exits at the PLS-198 F3 no-live-peer path before drain_all_items and the
        # HIVE-626 gate ever run (premerge run 9294: zero sweep lines, probe OK).
        # ken is a live Engineering peer but is fenced out of the SUBSTITUTION by
        # the HIVE-626 gate itself — the assertion still requires nova.
        available = {'ken@shizuha.com', 'nova@shizuha.com'}

        # Patch the METHOD on the shared singleton: tasks/tasks.py binds
        # `from .user_service import user_service` at import time, so replacing
        # the module attribute is invisible to the sweep (premerge run 9262 —
        # the resolver hit the real service, got ConnectionError, skipped the
        # compatible peer, and held instead of substituting).
        _fake = _fake_user_service()

        # Instrumentation round 3: capture sweep log output + resolver probe
        # result so a failure carries the exact filter that rejects nova.
        import logging

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.DEBUG)
                self.lines = []

            def emit(self, record):
                self.lines.append(f'{record.levelname} {record.name}: {record.getMessage()}')

        _h = _Capture()
        _root = logging.getLogger()
        _root.addHandler(_h)
        # Round-5 census fix: the sweep logs through the named `tasks` logger,
        # which settings.py sets propagate=False — a root-only capture NEVER
        # sees sweep verdicts (runs 9294/9502/9532: "zero sweep lines" was a
        # probe artifact, not a silent sweep). Attach to the named logger too.
        _tasks_logger = logging.getLogger('tasks')
        _tasks_logger.addHandler(_h)

        import tasks.tasks as _tt

        def _spy(name, orig):
            def _wrapped(*args, **kwargs):
                _v = orig(*args, **kwargs)
                _h.lines.append(f'SPY {name} args={args!r} kwargs={kwargs!r} -> {_v!r}')
                return _v
            return _wrapped

        def _verdict_spy(orig):
            def _wrapped(*args, **kwargs):
                _v = orig(*args, **kwargs)
                _team = args[1] if len(args) > 1 else kwargs.get('team_name')
                _email = args[2] if len(args) > 2 else kwargs.get('email')
                _h.lines.append(
                    f'SPY verdict team={_team!r} seat={_email!r} '
                    f'allowed={getattr(_v, "allowed", "?")} reasons={getattr(_v, "reasons", _v)!r}')
                return _v
            return _wrapped

        _tt_import = _tt
        _orig_hold = _tt._hive626_direct_review_hold
        _orig_resolver = _tt._resolve_direct_review_failover_target
        _orig_protect = _tt._soft_unavailable_owner_protected
        _orig_verdict = WorkflowService.__dict__['_seat_routability_verdict'].__func__
        _orig_new_assign = (
            WorkflowService.__dict__['_is_agent_available_for_new_assignment'].__func__
        )

        try:
            # b381ca2 (round-3 instrumentation edit) accidentally dropped the
            # patch wrapper entirely, so the sweep ran against the REAL user
            # service (ConnectionError in CI): the resolver skipped nova and
            # the sweep took the AC2 fail-loud hold (premerge run 9290).
            # Restore the wrapper; the diagnostics below are kept.
            # Also stub Redis: the probe of run 9502 showed the sweep emitting
            # side effects (notify_comment_mentions for "comment 160") against
            # the SHARED live service — a pod-level data-plane wedge, not a
            # sweep verdict. The stub makes the sweep deterministic.
            with patch('tasks.user_service.user_service.get_user_by_email',
                       side_effect=_fake.get_user_by_email), \
                 patch('tasks.user_service.user_service.get_user_by_id',
                       side_effect=_fake.get_user_by_id), \
                 patch.object(WorkflowService, '_redis_client',
                              return_value=_FakeRedis()), \
                 patch.object(WorkflowService, '_pod_roster', return_value=_ENG_ROSTER), \
                 patch.object(WorkflowService, '_is_agent_available_for_assignment',
                              side_effect=lambda e: e.lower() in available), \
                 patch.object(_tt, '_hive626_direct_review_hold',
                              _spy('hold', _orig_hold)), \
                 patch.object(_tt, '_resolve_direct_review_failover_target',
                              _spy('resolver', _orig_resolver)), \
                 patch.object(_tt, '_soft_unavailable_owner_protected',
                              _spy('protect', _orig_protect)), \
                 patch.object(WorkflowService, '_seat_routability_verdict',
                              staticmethod(_verdict_spy(_orig_verdict))), \
                 patch.object(WorkflowService, '_is_agent_available_for_new_assignment',
                              staticmethod(_spy('new_assign', _orig_new_assign))):
                sweep_rebalance_pods.run()
        except Exception as _exc:
            _h.lines.append(f'SWEEP EXCEPTION: {type(_exc).__name__}: {_exc}')
        finally:
            _root.removeHandler(_h)
            _tasks_logger.removeHandler(_h)

        _fresh = Item.objects.get(project=self.project)
        try:
            from tasks.tasks import _resolve_direct_review_failover_target as _rt
            with patch('tasks.user_service.user_service.get_user_by_email',
                       side_effect=_fake.get_user_by_email), \
                 patch('tasks.user_service.user_service.get_user_by_id',
                       side_effect=_fake.get_user_by_id):
                _probe = _rt(_fresh, 'sora@shizuha.com')
            _h.lines.append(f'RESOLVER PROBE: {_probe}')
        except Exception as _exc:
            _h.lines.append(f'PROBE EXCEPTION: {type(_exc).__name__}: {_exc}')
        _diag = '\n'.join(_h.lines[-80:])
        self.assertEqual(
            _fresh.assignee_email, 'nova@shizuha.com',
            'review must be substituted onto the resident Architecture peer;\n'
            f'sweep log:\n{_diag}',
        )

    @patch.dict(os.environ, _ENV)
    def test_hold_fail_loud_without_compatible_substitute(self):
        """AC2/AC4: with no compatible Architecture substitute, the review is
        held fail-loud on the down owner — never handed to Engineering."""
        self._review_item()
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ARCH_ROSTER_STARVED['architecture'],
                'is_active': True,
            },
        )
        # Round-7 fixture fix (runs 9586/9592/9600 census): with available=∅
        # BOTH roster members of the engineering pod read unavailable, the pod
        # has no live member, and the sweep exits at the PLS-198 F3
        # no-live-peer path BEFORE drain_all_items — the resolver-None branch
        # and the direct_review_authority hold are structurally unreachable
        # (zero HIVE-626 log lines in every census). AC2's fail-loud semantics
        # need the drain to RUN and find no compatible Architecture substitute:
        # ken stays available as the live peer that lets the drain start (he is
        # fenced out of the SUBSTITUTION by the gate itself — the assertions
        # still require sora to keep the item + the hold comment), while the
        # starved Architecture roster yields resolver (None, None).
        available = {'ken@shizuha.com'}

        _fake = _fake_user_service()

        # Round-6 census: AC2 flipped red at run 9586 with zero HIVE-626 log
        # lines — the drain never reached the hold. Wrap the decision seam and
        # carry the verdicts + the final comment census in the failure message.
        import logging

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.DEBUG)
                self.lines = []

            def emit(self, record):
                self.lines.append(f'{record.levelname} {record.name}: {record.getMessage()}')

        _h = _Capture()
        _root = logging.getLogger()
        _root.addHandler(_h)
        _tasks_logger = logging.getLogger('tasks')
        _tasks_logger.addHandler(_h)

        import tasks.tasks as _tt

        _orig_hold = _tt._hive626_direct_review_hold
        _orig_resolver = _tt._resolve_direct_review_failover_target
        _orig_protect = _tt._soft_unavailable_owner_protected

        def _spy(name, orig):
            def _wrapped(*args, **kwargs):
                _v = orig(*args, **kwargs)
                _h.lines.append(f'SPY {name} args={args!r} -> {_v!r}')
                return _v
            return _wrapped

        _orig_record_hold = WorkflowService.__dict__['_record_routability_hold'].__func__

        def _hold_spy(item, team, reasons, **kwargs):
            _h.lines.append(
                f'SPY record_hold item={getattr(item, "pk", "?")} team={team!r} '
                f'reasons={reasons!r} kwargs={kwargs!r}')
            return _orig_record_hold(item, team, reasons, **kwargs)

        try:
            with patch('tasks.user_service.user_service.get_user_by_email',
                       side_effect=_fake.get_user_by_email), \
                 patch('tasks.user_service.user_service.get_user_by_id',
                       side_effect=_fake.get_user_by_id), \
                 patch.object(WorkflowService, '_redis_client',
                              return_value=_FakeRedis()), \
                 patch.object(WorkflowService, '_pod_roster', return_value=_ENG_ROSTER), \
                 patch.object(WorkflowService, '_is_agent_available_for_assignment',
                              side_effect=lambda e: e.lower() in available), \
                 patch.object(_tt, '_hive626_direct_review_hold',
                              _spy('hold', _orig_hold)), \
                 patch.object(_tt, '_resolve_direct_review_failover_target',
                              _spy('resolver', _orig_resolver)), \
                 patch.object(_tt, '_soft_unavailable_owner_protected',
                              _spy('protect', _orig_protect)), \
                 patch.object(WorkflowService, '_record_routability_hold',
                              staticmethod(_hold_spy)):
                sweep_rebalance_pods.run()
        except Exception as _exc:
            _h.lines.append(f'SWEEP EXCEPTION: {type(_exc).__name__}: {_exc}')
        finally:
            _root.removeHandler(_h)
            _tasks_logger.removeHandler(_h)

        item = Item.objects.get(project=self.project)
        _comment_census = [
            c.content[:160] for c in Comment.objects.filter(item=item).order_by('created_at')
        ]
        _diag = '\n'.join(_h.lines[-80:])
        self.assertEqual(
            item.assignee_email, 'sora@shizuha.com',
            'review must stay with its direct owner when no compatible '
            'substitute exists (fail-loud hold, not Engineering fallback);\n'
            f'sweep log:\n{_diag}',
        )
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                content__contains='hive-626-direct-review',
            ).exists(),
            'a hive-626-direct-review routability hold must be recorded;\n'
            f'comments={_comment_census!r}\n'
            f'sweep log:\n{_diag}',
        )

    @patch.dict(os.environ, _ENV)
    def test_normal_engineering_failover_still_drains(self):
        """AC5: the same down owner's plain Engineering task (no review label)
        still fails over to the available Engineering peer."""
        plain = Item.objects.create(
            title='Implement storage-redundancy guard follow-up',
            mode='task', status='in_progress', priority='urgent',
            project=self.project,
            assignment_group='engineering',
            created_by_email='ken@shizuha.com',
            assignee_id=204, assignee_email='sora@shizuha.com',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ARCH_ROSTER_STARVED['architecture'],
                'is_active': True,
            },
        )
        available = {'ken@shizuha.com'}

        _fake = _fake_user_service()
        with patch('tasks.user_service.user_service.get_user_by_email',
                   side_effect=_fake.get_user_by_email), \
             patch('tasks.user_service.user_service.get_user_by_id',
                   side_effect=_fake.get_user_by_id), \
             patch.object(WorkflowService, '_redis_client',
                          return_value=_FakeRedis()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ENG_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            sweep_rebalance_pods.run()

        plain.refresh_from_db()
        self.assertEqual(
            plain.assignee_email, 'ken@shizuha.com',
            'normal compatible failover must continue to work',
        )
