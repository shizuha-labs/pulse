"""PLS-675 / PLAT-1351 v4: deploy-verify close gate.

Typed ``acceptance_mode`` (code|deployed|live) is the sole enforcement
authority. Merge completion is NOT deployment verification. Free-form
comments are never waiver or live-proof authority. Terminal close for
deployed/live requires a correlated production DeploymentEvent
(task + repo + merged head SHA + env + image/digest + replicas Ready);
live also needs a typed QA/live-probe verdict. Role-gated durable
waivers only; automation cannot issue or consume them.

Kill-switch: PULSE_DEPLOY_VERIFY_GATE_ENABLED.
"""
from unittest import mock
from contextlib import ExitStack, contextmanager

import os
import uuid

from django.test import SimpleTestCase, TestCase, override_settings

from tasks.models import (
    Comment,
    DeploymentEvent,
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
MERGED_SHA_SHORT = 'deadbeefcafe'
CORRECTIVE_SHA = 'feedfacecafe1234567890abcdef1234567890ab'
REPO = 'shizuha-labs/x'


class _DictRedis:
    """Small hash/string Redis fake for per-task retry liveness."""
    def __init__(self):
        self.strings = {}
        self.hashes = {}

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def hdel(self, key, field):
        self.hashes.setdefault(key, {}).pop(field, None)



def _isolated_liveness_client(db=15):
    """Return a Redis client on a dedicated test DB so merge-event retry-failure
    assertions never race with other parallel workers sharing the default DB.

    The liveness module's ``_redis_client`` is cached module-globally; patching
    it for the duration of a test routes every internal record/read/clear (e.g.
    inside ``sync_linked_pr_statuses.run``) to this isolated store.

    Derives the host from ``REDIS_URL`` (the real job-local test Redis), NOT
    ``CELERY_BROKER_URL``: the base settings override the broker to the ``:9``
    discard sentinel under TESTING, so a broker-derived base would point at a
    port that refuses connections and the liveness assertions would always see
    "no failure".

    PLAT-5343: both liveness legs swallow store errors (DEBUG-log + return
    None), so a transiently unavailable job-local redis sidecar previously
    surfaced as ``assertIsNotNone(failure) → None`` with zero attribution
    (merge 06f4dbf class). The client therefore pings fail-loud with a
    descriptive error, and runs saner timeouts (5s + retry_on_timeout) so a
    slow bind is tolerated instead of silently dropped.
    """
    import redis
    from django.conf import settings

    url = (getattr(settings, 'REDIS_URL', None)
           or os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/5'))
    # redis-py's from_url lets the URL path (/N) win over the db= kwarg, so
    # strip any trailing /N before forcing the dedicated test DB.
    base = url.rsplit('/', 1)[0] if '/' in url.split('://', 1)[-1] else url
    client = redis.Redis.from_url(
        base, db=db, decode_responses=True, socket_timeout=5,
        socket_connect_timeout=5, retry_on_timeout=True,
    )
    try:
        client.ping()
    except Exception as exc:
        raise AssertionError(
            'PLAT-5343: isolated liveness Redis (db=%s) at %s is unavailable '
            '(ping failed: %s) — the job-local redis-server sidecar is not '
            'healthy. Both liveness legs swallow store errors, so without '
            'this gate the failure surfaces as a bare assertIsNotNone(None) '
            'with no attribution.' % (db, base, exc)
        ) from exc
    return client


@contextmanager
def _patch_isolated_liveness(db=15):
    """Keep each inherited test invocation separate within the real Redis DB."""
    import tasks.liveness as liveness_mod

    client = _isolated_liveness_client(db=db)
    namespace = f'fixture:{uuid.uuid4().hex}'
    keys = {
        name: f'{namespace}:{name.lower()}'
        for name in (
            'MERGE_EVENT_RETRY_FAILURES_HASH',
            'MERGE_EVENT_RETRY_FAILURE_KEY',
            'MERGE_EVENT_RETRY_PENDING_KEY',
        )
    }
    try:
        with mock.patch.multiple(liveness_mod, **keys), mock.patch.object(
            liveness_mod, '_redis_client', return_value=client,
        ):
            yield client
    finally:
        client.delete(*keys.values())


class LivenessFixtureIsolationTests(SimpleTestCase):
    def test_shared_keys_reproduce_cross_worker_failure_erasure(self):
        from tasks.liveness import (
            clear_merge_event_retry_failure, read_merge_event_retry_failure,
            record_merge_event_retry_failure,
        )

        shared = _DictRedis()
        record_merge_event_retry_failure(item_key='PLAT-1', error='worker A', client=shared)
        record_merge_event_retry_failure(item_key='PLAT-1', error='worker B', client=shared)
        clear_merge_event_retry_failure(item_key='PLAT-1', client=shared)
        self.assertIsNone(read_merge_event_retry_failure(client=shared))

    def test_inherited_invocations_cannot_clear_each_others_pending_failure(self):
        from tasks.liveness import (
            clear_merge_event_retry_failure, read_merge_event_retry_failure,
            record_merge_event_retry_failure,
        )

        shared = _DictRedis()
        with mock.patch(f'{__name__}._isolated_liveness_client', return_value=shared):
            with _patch_isolated_liveness(db=14):
                record_merge_event_retry_failure(item_key='PLAT-1', error='worker A')
                with _patch_isolated_liveness(db=14):
                    record_merge_event_retry_failure(item_key='PLAT-1', error='worker B')
                    clear_merge_event_retry_failure(item_key='PLAT-1')
                    self.assertIsNone(read_merge_event_retry_failure())
                failure = read_merge_event_retry_failure()
                self.assertIsNotNone(failure)
                self.assertEqual(failure['error'], 'worker A')
                self.assertEqual(failure['pending_count'], 1)
        self.assertEqual(shared.hashes, {})
        self.assertEqual(shared.strings, {})



class DeployVerifyGateV4Tests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 1
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        self.record_heartbeat = patcher.start()
        self.addCleanup(patcher.stop)
        self._redis = _DictRedis()
        redis_patcher = mock.patch(
            'tasks.liveness._redis_client', return_value=self._redis,
        )
        redis_patcher.start()
        self.addCleanup(redis_patcher.stop)
        # PLS-1395: a runner token must not turn stored-status fixtures into
        # live Origin reads. Tests that need the authoritative path set the
        # token themselves.
        for name in (
            'GITHUB_TOKEN', 'FORGEJO_TOKEN', 'ORIGIN_TOKEN', 'GITEA_TOKEN', 'TEA_TOKEN',
        ):
            self.addCleanup(lambda n=name, v=os.environ.get(name): (
                os.environ.__setitem__(n, v) if v is not None else os.environ.pop(n, None)
            ))
            os.environ.pop(name, None)

    # ── factories ────────────────────────────────────────────────────────────

    def _merged_pr(
        self, *, sha=MERGED_SHA, merge_sha='', repo=REPO, status='merged',
    ):
        entry = {
            'url': f'https://origin.shizuha.com/{repo}/pulls/1',
            'repo': repo,
            'number': 1,
            'status': status,
            'merged_at': '2026-07-03T00:00:00Z',
            'head_sha': sha,
        }
        if merge_sha:
            entry['merge_commit_sha'] = merge_sha
        return entry

    def _open_pr(self, *, sha='abc123open', repo=REPO):
        return {
            'url': f'https://origin.shizuha.com/{repo}/pulls/2',
            'repo': repo,
            'status': 'open',
            'head_sha': sha,
        }

    def _corrective_pr(
        self, *, sha=CORRECTIVE_SHA, repo=REPO, status='open', url=None,
    ):
        return {
            'url': url or f'https://origin.shizuha.com/{repo}/pulls/2',
            'repo': repo,
            'number': 2,
            'status': status,
            'head_sha': sha,
        }

    def _item(
        self,
        *,
        labels=None,
        tags='',
        workflow_name='autonomous-bug',
        acceptance_mode=None,
        prs='merged',
        status='in_review',
        metadata=None,
        **kw,
    ):
        if metadata is None:
            metadata = {}
        else:
            metadata = dict(metadata)
        if prs == 'merged':
            metadata.setdefault('pull_requests', [self._merged_pr()])
        elif prs == 'open':
            metadata.setdefault('pull_requests', [self._open_pr()])
        elif prs == 'none':
            metadata.setdefault('pull_requests', [])
        elif isinstance(prs, list):
            metadata.setdefault('pull_requests', prs)
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        create_kw = dict(
            project=self.project,
            title='[BUG] prod 500 on close path',
            description='Service-affecting regression in production.',
            labels=labels if labels is not None else [],
            tags=tags,
            status=status,
            workflow_name=workflow_name,
            item_type='task',
            created_by_id=1,
            created_by_email='jun@shizuha.com',
            metadata=metadata,
        )
        if acceptance_mode is not None:
            create_kw['acceptance_mode'] = acceptance_mode
        create_kw.update(kw)
        return Item.objects.create(**create_kw)

    def _comment(self, item, text, *, email='jun@shizuha.com'):
        return Comment.objects.create(
            item=item, content=text, author_id=1, author_email=email,
        )

    def _actor(self, email='qa-lead@shizuha.com'):
        return type('Actor', (), {'id': 7, 'email': email})()

    def _seed_governed_merge_workflow(self, slug='autonomous-bug'):
        """Seed awaiting_merge → awaiting_deploy so real execute_transition can run.

        Rei P2 production-order fixture requires a real seeded workflow transition
        (not a status-writing stub). Mirrors migration 0142's governed path.
        """
        statuses = {}
        for position, (status_slug, category) in enumerate(
            (
                ('open', 'todo'),
                ('in_progress', 'in_progress'),
                ('in_review', 'in_progress'),
                ('awaiting_merge', 'in_progress'),
                ('awaiting_deploy', 'in_progress'),
                ('done', 'done'),
            ),
            start=1,
        ):
            status, _ = Status.objects.get_or_create(
                slug=status_slug,
                defaults={
                    'name': status_slug.replace('_', ' ').title(),
                    'category': category,
                    'position': position,
                    'is_system': True,
                },
            )
            # Keep category correct if a prior test created the slug differently.
            if status.category != category:
                status.category = category
                status.save(update_fields=['category'])
            statuses[status_slug] = status

        workflow, _ = Workflow.objects.get_or_create(
            slug=slug,
            defaults={
                'name': slug.replace('-', ' ').title(),
                'initial_status': statuses['open'],
                'is_system': True,
            },
        )
        for position, status_slug in enumerate(statuses, start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=statuses[status_slug],
                defaults={'position': position},
            )
        WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            from_status='awaiting_merge',
            to_status='awaiting_deploy',
            defaults={
                'name': 'Merge — Awaiting Deploy',
                'position': 35,
                'button_style': 'primary',
            },
        )
        WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            from_status='awaiting_deploy',
            to_status='done',
            defaults={
                'name': 'Deploy Verified — Close',
                'position': 36,
                'button_style': 'success',
            },
        )
        WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            from_status='awaiting_merge',
            to_status='done',
            defaults={
                'name': 'Merge — Complete Code-Only Work',
                'position': 37,
                'button_style': 'success',
            },
        )
        return workflow

    def _seed_security_merge_workflow(self):
        """Seed the live security-finding Merge → Verification contract."""
        statuses = {}
        for position, (status_slug, category) in enumerate((
            ('awaiting_merge', 'in_progress'),
            ('verification', 'in_progress'),
            ('completed', 'done'),
        ), start=1):
            status, _ = Status.objects.get_or_create(
                slug=status_slug,
                defaults={
                    'name': status_slug.replace('_', ' ').title(),
                    'category': category,
                    'position': position,
                    'is_system': True,
                },
            )
            if status.category != category:
                status.category = category
                status.save(update_fields=['category'])
            statuses[status_slug] = status
        workflow, _ = Workflow.objects.get_or_create(
            slug='security-finding',
            defaults={
                'name': 'Security Finding',
                'initial_status': statuses['awaiting_merge'],
                'is_system': True,
            },
        )
        for position, status_slug in enumerate(statuses, start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=statuses[status_slug],
                defaults={'position': position},
            )
        transition, _ = WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            from_status='awaiting_merge',
            to_status='verification',
            defaults={
                'name': 'Merge — Verify Fixed',
                'position': 35,
                'button_style': 'primary',
            },
        )
        return workflow, transition

    def _seed_corrective_review_workflow(self, slug='autonomous-bug'):
        workflow = self._seed_governed_merge_workflow(slug)
        corrective, _ = WorkflowTransition.objects.update_or_create(
            workflow=workflow,
            name='Submit Corrective PR for Code Review',
            from_status='awaiting_deploy',
            to_status='in_review',
            defaults={
                'position': 34,
                'button_style': 'primary',
                'validators': [
                    {'type': 'awaiting_deploy_corrective_pr'},
                    {'type': 'submit_requires_current_direction'},
                ],
                'post_functions': [
                    {'type': 'stash_implementer'},
                    {'type': 'assign_team', 'value': 'review'},
                    {'type': 'stamp_review_started'},
                ],
            },
        )
        review_passed, _ = WorkflowTransition.objects.update_or_create(
            workflow=workflow,
            name='Review Passed — Awaiting Merge',
            from_status='in_review',
            to_status='awaiting_merge',
            defaults={
                'position': 35,
                'button_style': 'primary',
                'post_functions': [
                    {'type': 'assign_team', 'value': 'merge'},
                ],
            },
        )
        merge = WorkflowTransition.objects.get(
            workflow=workflow,
            from_status='awaiting_merge',
            to_status='awaiting_deploy',
        )
        close = WorkflowTransition.objects.get(
            workflow=workflow,
            from_status='awaiting_deploy',
            to_status='done',
        )
        return corrective, review_passed, merge, close

    @staticmethod
    def _confirmed_prs(item, *, confirmed=True):
        return False, {
            str(pr.get('url') or '').strip().rstrip('/'): confirmed
            for pr in (item.metadata or {}).get('pull_requests', [])
            if pr.get('url')
        }

    @staticmethod
    def _set_test_owner(
        item, *, user_id=11, email='author@shizuha.com', group='devops',
    ):
        # These focused workflow tests exercise transition-time routing rather
        # than the separate Item assignment API.  Seed its already-authorized
        # source owner directly so the model's live TeamRoster guard does not
        # make the fixture depend on mutable fleet roster data.
        rosters = {
            'devops': [['author@shizuha.com']],
            'review': [['reviewer@shizuha.com']],
            'merge': [['merger@shizuha.com']],
        }
        for team_name, tiers in rosters.items():
            TeamRoster.objects.update_or_create(
                organization_id=item.project.org_id,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': tiers,
                    'is_active': True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()
        Item.objects.filter(pk=item.pk).update(
            assignee_id=user_id,
            assignee_email=email,
            assignment_group=group,
        )
        item.refresh_from_db()
        return item

    @staticmethod
    def _live_probe_payload_fields(item_key, *, email='qa-lead@shizuha.com', evidence='x'):
        """Minimal valid live-probe payload including deployment bind fields."""
        return {
            'item_key': item_key,
            'result': 'passed',
            'issuer_email': email,
            'issuer_team': 'qa',
            'issued_at': '2026-08-01T00:00:00Z',
            'evidence': evidence,
            'deployment_event_id': '00000000-0000-0000-0000-000000000001',
            'deployment_repo': REPO,
            'deployment_commit_sha': MERGED_SHA,
            'deployment_image': 'registry.example/x:abc',
            'deployment_image_digest': '',
            'deployment_image_tag': '',
            'deployment_replicas_ready': True,
            'deployment_converged_at': '2026-08-01T00:00:00Z',
        }

    def _live_probe_receipt(
        self, item, *, email='qa-lead@shizuha.com', evidence='GET /health 200',
        deployment_event=None,
    ):
        """Mint an immutable typed live-probe receipt (role-gated path).

        Requires a correlated production deployment (created here if omitted)
        so the receipt binds to that exact deploy identity.
        """
        ev = deployment_event
        if ev is None:
            ev = self._deploy_event(item, live_probe=False)
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            return WorkflowService.issue_live_probe_receipt(
                item, actor=self._actor(email), evidence=evidence,
                event_id=f'test-live-probe-{item.item_key}-{email}-{ev.pk}',
                deployment_event=ev,
            )

    def _waiver_receipt(self, item, *, email='devops@shizuha.com', reason='config-only'):
        """Mint an immutable typed deploy-verify waiver receipt."""
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            return WorkflowService.issue_deploy_verify_waiver(
                item, actor=self._actor(email), reason=reason,
                event_id=f'test-waiver-{item.item_key}-{email}',
                evidence={
                    'kind': 'inline_package',
                    'actor': email,
                    'originating_key': item.item_key,
                    'reason': reason,
                    'target_head': MERGED_SHA,
                    'verdict_refs': ['focused-qa:PASS'],
                    'scope_conditions': ['config-only change'],
                },
            )

    def _deploy_event(
        self,
        item,
        *,
        environment='production',
        state='smoke_passed',
        smoke_result='passed',
        commit_sha=MERGED_SHA,
        repo=REPO,
        image='registry.example/x:abc',
        image_digest='',
        image_tag='',
        replicas_ready=True,
        live_probe=False,
        source_task_keys=None,
        extra_metadata=None,
    ):
        meta = {}
        if replicas_ready is True:
            meta['replicas_ready'] = True
        elif isinstance(replicas_ready, dict):
            meta.update(replicas_ready)
        elif replicas_ready is False:
            meta['replicas_ready'] = False
        if live_probe is True:
            meta['live_probe'] = {
                'schema': 'shizuha.pulse.live-probe.v1',
                'result': 'passed',
                'evidence': 'https://prod/x health 200',
                'verified_by': 'qa@shizuha.com',
            }
        elif isinstance(live_probe, dict):
            meta['live_probe'] = live_probe
        if extra_metadata:
            meta.update(extra_metadata)
        return DeploymentEvent.objects.create(
            idempotency_key=(
                f'k-{item.item_key}-{environment}-{state}-'
                f'{commit_sha[:12]}-{DeploymentEvent.objects.count()}'
            ),
            project=self.project,
            repo=repo,
            commit_sha=commit_sha,
            service='x',
            environment=environment,
            state=state,
            smoke_result=smoke_result,
            image=image or '',
            image_digest=image_digest or '',
            image_tag=image_tag or '',
            source_task_keys=(
                source_task_keys if source_task_keys is not None
                else [item.item_key]
            ),
            metadata=meta,
        )

    def _waiver(self, *, issued_by='devops@shizuha.com', reason='config-only'):
        return {
            'schema': 'shizuha.pulse.deploy-verify-waiver.v1',
            'reason': reason,
            'issued_by': issued_by,
        }

    # ── resolve / merge target ───────────────────────────────────────────────

    def test_resolve_explicit_field_wins(self):
        item = self._item(acceptance_mode='deployed', labels=['service-affecting'])
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'deployed',
        )

    def test_create_backfills_acceptance_mode_from_metadata_mirror(self):
        # Rei P2-5: on create, a valid metadata.acceptance_mode is written
        # through onto the field; enforcement then reads the field only.
        item = self._item(
            acceptance_mode=None,
            metadata={'acceptance_mode': 'live'},
            labels=[],
        )
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'live')
        self.assertEqual(WorkflowService._resolve_acceptance_mode(item), 'live')

    def test_create_persists_service_affecting_default_live(self):
        # Rei P2-5: typed default is persisted at insert, not left null.
        item = self._item(labels=['service-affecting'])
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'live')

    def test_create_persists_plain_default_code(self):
        item = self._item(labels=['docs'])
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'code')

    def test_field_wins_over_metadata_mirror(self):
        # Rei P2-5: the field is the sole authority — a metadata mirror cannot
        # override an explicit field value (even at create).
        item = self._item(
            acceptance_mode='code',
            metadata={'acceptance_mode': 'live'},
            labels=[],
        )
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'code')
        self.assertEqual(WorkflowService._resolve_acceptance_mode(item), 'code')

    def test_resolve_service_affecting_defaults_live(self):
        item = self._item(labels=['service-affecting'])
        self.assertEqual(WorkflowService._resolve_acceptance_mode(item), 'live')

    def test_resolve_plain_defaults_code(self):
        item = self._item(labels=['docs'])
        self.assertEqual(WorkflowService._resolve_acceptance_mode(item), 'code')

    def test_merge_event_target_code_is_done(self):
        self._seed_governed_merge_workflow('autonomous-bug')
        item = self._item(
            acceptance_mode='code', labels=[], status='awaiting_merge',
        )
        self.assertEqual(
            WorkflowService.merge_event_target_status(item), 'done',
        )

    def test_merge_event_target_deployed_is_awaiting_deploy(self):
        self._seed_governed_merge_workflow('autonomous-bug')
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge',
        )
        self.assertEqual(
            WorkflowService.merge_event_target_status(item), 'awaiting_deploy',
        )

    def test_merge_event_target_live_default_is_awaiting_deploy(self):
        self._seed_governed_merge_workflow('autonomous-bug')
        item = self._item(
            labels=['service-affecting'], status='awaiting_merge',
        )
        self.assertEqual(
            WorkflowService.merge_event_target_status(item), 'awaiting_deploy',
        )

    def test_merge_event_target_uses_security_workflow_edge(self):
        self._seed_security_merge_workflow()
        item = self._item(
            acceptance_mode='live',
            workflow_name='security-finding',
            status='awaiting_merge',
        )
        self.assertEqual(
            WorkflowService.merge_event_target_status(item), 'verification',
        )

    def test_merge_event_target_refuses_invented_workflow_status(self):
        workflow, transition = self._seed_security_merge_workflow()
        transition.delete()
        item = self._item(
            acceptance_mode='live',
            workflow_name=workflow.slug,
            status='awaiting_merge',
        )
        self.assertIsNone(WorkflowService.merge_event_target_status(item))

    def test_code_merge_target_refuses_unrelated_transition(self):
        workflow = self._seed_governed_merge_workflow('autonomous-bug')
        WorkflowTransition.objects.filter(
            workflow=workflow,
            from_status='awaiting_merge',
            to_status='done',
        ).delete()
        item = self._item(
            acceptance_mode='code', labels=[],
            workflow_name=workflow.slug,
            status='awaiting_merge',
        )
        self.assertIsNone(WorkflowService.merge_event_target_status(item))

    # ── not gated / code mode ────────────────────────────────────────────────

    def test_non_terminal_target_not_gated(self):
        item = self._item(acceptance_mode='live', labels=['service-affecting'])
        WorkflowService._validate_deploy_verify_gate(item, 'in_progress')

    def test_code_mode_closes_without_deploy_evidence(self):
        item = self._item(acceptance_mode='code', labels=[], prs='merged')
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_code')
        self.assertEqual(liveness['acceptance_mode'], 'code')
        self.assertEqual(liveness['missing_classes'], [])

    def test_plain_work_defaults_code_and_closes(self):
        item = self._item(labels=['ui', 'copy'], prs='none')
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    # ── no-pr / no-linked-PR are NOT success bypasses for governed modes ─────

    def test_no_linked_pr_does_not_exempt_deployed(self):
        item = self._item(acceptance_mode='deployed', prs='none', labels=[])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_no_pr_label_does_not_exempt_live(self):
        item = self._item(
            acceptance_mode='live',
            labels=['service-affecting', 'no-pr'],
            prs='none',
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_focused_qa_projects_origin_deploy_and_review_verdict(self):
        """PLS-888: no-PR QA closes from its typed origin/event projection.

        Production order: origin deploy already verified -> generated focused-QA
        task -> independent Review Approve.  The QA item has no PR of its own,
        so requiring a task-local merged head would be unsatisfiable.
        """
        origin = self._item(acceptance_mode='live', prs='merged')
        event = self._deploy_event(origin, state='verified_by_qa')
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_origin_item_key': origin.item_key,
                'focused_qa_deployment_event_id': str(event.pk),
            },
        )
        WorkflowService._validate_deploy_verify_gate(qa, 'done')

    def test_focused_qa_projection_requires_exact_origin_event_membership(self):
        """PLS-888 negative control: an unrelated event UUID cannot be reused."""
        origin = self._item(acceptance_mode='live', prs='merged')
        unrelated = self._item(acceptance_mode='live', prs='merged')
        event = self._deploy_event(unrelated, state='verified_by_qa')
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_origin_item_key': origin.item_key,
                'focused_qa_deployment_event_id': str(event.pk),
            },
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(qa, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_focused_qa_projection_cannot_cross_organizations(self):
        """PLS-888 negative control: typed projection stays org-scoped."""
        origin = self._item(acceptance_mode='live', prs='merged')
        event = self._deploy_event(origin, state='verified_by_qa')
        other_project = Project.objects.create(
            name='Other', key='OTH', org_id=2,
            access_level='private', is_active=True,
        )
        qa = self._item(
            project=other_project,
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_origin_item_key': origin.item_key,
                'focused_qa_deployment_event_id': str(event.pk),
            },
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(qa, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_focused_qa_without_verified_origin_deploy_stays_closed(self):
        """PLS-888 negative control: provenance without success cannot close."""
        origin = self._item(acceptance_mode='live', prs='merged')
        event = self._deploy_event(
            origin, state='rollout_started', smoke_result='pending',
        )
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={
                'focused_qa_origin_item_id': str(origin.pk),
                'focused_qa_origin_item_key': origin.item_key,
                'focused_qa_deployment_event_id': str(event.pk),
            },
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(qa, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_ordinary_no_pr_live_task_cannot_use_untyped_projection(self):
        """PLS-888 negative control: only producer-authored QA types project."""
        origin = self._item(acceptance_mode='live', prs='merged')
        event = self._deploy_event(origin, state='verified_by_qa')
        item = self._item(
            acceptance_mode='live',
            prs='none',
            metadata={'deployment_event_id': str(event.pk)},
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_focused_qa_direct_push_binds_via_event_qa_task(self):
        """PLS-888 extension: direct-push/CI-only focused-QA closes via the
        deployment event's own qa_task binding when the event is verified_by_qa.

        A direct push to main (no originating PR task) cannot project from an
        origin item. The focused-QA automation still binds the generated item
        as the DeploymentEvent.qa_task; a verified_by_qa event whose qa_task is
        EXACTLY this item is the deploy system's own QA-verification signal and
        is sufficient correlation. The qa_task FK is automation-set, not
        task-editor-writable, so it cannot be forged via item metadata.
        """
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={
                'focused_qa_deployment_event_id': '00000000-0000-0000-0000-000000000099',
            },
        )
        event = DeploymentEvent.objects.create(
            idempotency_key=f'k-direct-push-{qa.item_key}-{DeploymentEvent.objects.count()}',
            project=self.project,
            repo=REPO,
            commit_sha=MERGED_SHA,
            service='x',
            environment='live',
            state='verified_by_qa',
            smoke_result='passed',
            image='registry.example/x:abc',
            source_task_keys=[],
            metadata={'replicas_ready': True},
            qa_task=qa,
        )
        qa.metadata = {
            **qa.metadata,
            'focused_qa_deployment_event_id': str(event.pk),
        }
        qa.save(update_fields=['metadata'])
        WorkflowService._validate_deploy_verify_gate(qa, 'done')

    def test_focused_qa_direct_push_requires_exact_qa_task_binding(self):
        """PLS-888 negative control: a verified_by_qa event bound to a DIFFERENT
        qa_task cannot close a direct-push focused-QA item."""
        other = self._item(acceptance_mode='live', prs='none')
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={'focused_qa_deployment_event_id': '00000000-0000-0000-0000-000000000098'},
        )
        event = DeploymentEvent.objects.create(
            idempotency_key=f'k-direct-push-other-{qa.item_key}-{DeploymentEvent.objects.count()}',
            project=self.project,
            repo=REPO,
            commit_sha=MERGED_SHA,
            service='x',
            environment='live',
            state='verified_by_qa',
            smoke_result='passed',
            image='registry.example/x:abc',
            source_task_keys=[],
            metadata={'replicas_ready': True},
            qa_task=other,
        )
        qa.metadata = {
            **qa.metadata,
            'focused_qa_deployment_event_id': str(event.pk),
        }
        qa.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(qa, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    def test_focused_qa_direct_push_non_verified_event_stays_closed(self):
        """PLS-888 negative control: a smoke_passed (not verified_by_qa) event
        bound via qa_task cannot close a direct-push focused-QA item."""
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.focused_qa',
            source='focused-qa-automation',
            metadata={'focused_qa_deployment_event_id': '00000000-0000-0000-0000-000000000097'},
        )
        event = DeploymentEvent.objects.create(
            idempotency_key=f'k-direct-push-smoke-{qa.item_key}-{DeploymentEvent.objects.count()}',
            project=self.project,
            repo=REPO,
            commit_sha=MERGED_SHA,
            service='x',
            environment='live',
            state='smoke_passed',
            smoke_result='passed',
            image='registry.example/x:abc',
            source_task_keys=[],
            metadata={'replicas_ready': True},
            qa_task=qa,
        )
        qa.metadata = {
            **qa.metadata,
            'focused_qa_deployment_event_id': str(event.pk),
        }
        qa.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(qa, 'done')
        self.assertIn('missing_live_probe', str(ctx.exception))

    def test_regression_watchlist_projects_exact_successful_event(self):
        """PLS-888: DeploymentEvent watchlists inherit that exact event only."""
        event = self._deploy_event(
            self._item(acceptance_mode='live', prs='merged'),
            state='verified_by_qa',
        )
        qa = self._item(
            acceptance_mode='live',
            prs='none',
            workflow_name='reviewed-task',
            item_type='task.regression_watchlist',
            source='deployment-event',
            metadata={
                'deployment_qa_kind': 'regression-watchlist',
                'deployment_event_id': str(event.pk),
            },
        )
        WorkflowService._validate_deploy_verify_gate(qa, 'done')

    # ── open PR blocks terminal (PLAT-4719) ──────────────────────────────────

    def test_open_linked_pr_blocks_code_mode(self):
        item = self._item(acceptance_mode='code', prs='open')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('open_linked_pr', str(ctx.exception))

    def test_open_linked_pr_blocks_even_with_waiver(self):
        item = self._item(
            acceptance_mode='deployed',
            prs='open',
            metadata={'deploy_verify_waiver': self._waiver()},
        )
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('open_linked_pr', str(ctx.exception))

    # ── PLAT-5854: merged Origin PR supersedes a stale GitHub-mirror PR ─────
    # A retired GitHub-mirror PR (github.com) is a read-only duplicate and can
    # never be delivery authority (PLS-765, ORIG-18). When the same task has a
    # merged Origin PR for the fix, a stale open mirror PR is NOT an open
    # obligation — it must not wedge terminal close (PLS-466/CON-225 wedges).
    # Fail-closed preserved: only-an-open-PR (Origin OR mirror, no merged
    # Origin delivery) still blocks.

    def test_merged_origin_pr_supersedes_open_mirror_pr(self):
        item = self._item(
            acceptance_mode='code',
            prs=[
                self._merged_pr(sha=MERGED_SHA),
                {
                    'url': 'https://github.com/shizuha-labs/x/pull/528',
                    'repo': REPO,
                    'status': 'open',
                },
            ],
        )
        self.assertFalse(WorkflowService._has_open_linked_prs(item))
        # Code mode: no deploy event needed; merged Origin + stale mirror open
        # must close without the open_linked_pr class.
        self.assertNotIn(
            'open_linked_pr',
            WorkflowService._collect_deploy_verify_missing(item, 'code'),
        )

    def test_open_mirror_pr_without_merged_origin_still_blocks(self):
        # Negative control: a task whose ONLY obligation is an open PR (even a
        # github.com mirror) has no merged Origin delivery — fail-closed.
        item = self._item(
            acceptance_mode='code',
            prs=[{
                'url': 'https://github.com/shizuha-labs/x/pull/528',
                'repo': REPO,
                'status': 'open',
            }],
        )
        self.assertTrue(WorkflowService._has_open_linked_prs(item))
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('open_linked_pr', str(ctx.exception))

    def test_open_origin_pr_alongside_merged_origin_still_blocks(self):
        # Only a MIRROR duplicate is superseded; a genuinely open Origin PR for
        # a different change still blocks close (fail-closed, PLAT-4719).
        item = self._item(
            acceptance_mode='code',
            prs=[
                self._merged_pr(sha=MERGED_SHA),
                self._open_pr(),
            ],
        )
        self.assertTrue(WorkflowService._has_open_linked_prs(item))

    # ── PLS-466: verification ENTRY requires a merged PR ────────────────────
    # Retro 12Jul prevention: a task carrying linked PRs must not advance into
    # the verification lane while its fix PR is open or closed-unmerged. The
    # PLAT-4647 recurrence (security-finding awaiting_merge → verification with
    # its Origin PR still open) is the fixture this closes.

    def test_open_pr_blocks_verification_entry(self):
        item = self._item(prs='open')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_verification_pr_gate(item, 'verification')
        msg = str(ctx.exception)
        self.assertIn('verification', msg)
        self.assertIn('not merged', msg)
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_target'], 'verification')
        self.assertEqual(liveness['last_result'], 'block_open_pr')
        self.assertIn(
            'open_linked_pr_not_merged', liveness['missing_classes'],
        )

    def test_open_pr_blocks_awaiting_verification_entry(self):
        item = self._item(prs='open')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_verification_pr_gate(
                item, 'awaiting_verification',
            )
        self.assertIn('awaiting_verification', str(ctx.exception))

    def test_closed_unmerged_pr_blocks_verification_entry(self):
        item = self._item(prs=[{
            'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/3',
            'repo': REPO,
            'status': 'closed',
            'merged_at': None,
            'head_sha': 'abc123closed',
        }])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_verification_pr_gate(item, 'verification')
        self.assertIn('closed', str(ctx.exception))

    def test_mirror_closed_github_pr_blocks_verification_entry(self):
        # A GitHub-mirror PR that is closed/unmerged is NOT delivery evidence
        # (PLS-765: only a merged Origin PR counts). It must still block.
        item = self._item(prs=[{
            'url': 'https://github.com/shizuha-labs/x/pull/4',
            'repo': 'shizuha-labs/x',
            'status': 'closed',
            'merged_at': None,
            'head_sha': 'abc123mirror',
        }])
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_verification_pr_gate(item, 'verification')

    def test_merged_origin_pr_allows_verification_entry(self):
        item = self._item(prs='merged')
        # No exception: a genuinely merged Origin PR satisfies the gate.
        self.assertIsNone(
            WorkflowService._validate_verification_pr_gate(item, 'verification'),
        )

    def test_no_linked_pr_not_governed(self):
        item = self._item(prs='none')
        self.assertIsNone(
            WorkflowService._validate_verification_pr_gate(item, 'verification'),
        )

    def test_non_verification_target_ignored(self):
        # The gate only fires on the verification lane; terminal close is the
        # deploy-verify gate's job, not this one.
        item = self._item(prs='open')
        self.assertIsNone(
            WorkflowService._validate_verification_pr_gate(item, 'done'),
        )

    def test_multiple_prs_one_merged_allows(self):
        item = self._item(prs=[
            self._open_pr(),
            self._merged_pr(),
        ])
        self.assertIsNone(
            WorkflowService._validate_verification_pr_gate(item, 'verification'),
        )

    def test_multiple_prs_all_open_blocks(self):
        item = self._item(prs=[self._open_pr(), self._open_pr()])
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_verification_pr_gate(item, 'verification')

    def test_pls1395_live_refresh_promotes_stale_open_to_merged(self):
        item = self._item(prs='open')
        url = item.metadata['pull_requests'][0]['url']

        def _refresh(target):
            prs = target.metadata['pull_requests']
            prs[0]['status'] = 'merged'
            prs[0]['last_synced_at'] = '2026-10-09T06:22:00+00:00'
            target.metadata['pull_requests'] = prs
            target.save(update_fields=['metadata'])
            return True, {url.rstrip('/'): True}

        with mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}, clear=False):
            with mock.patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                side_effect=_refresh,
            ):
                self.assertIsNone(
                    WorkflowService._validate_verification_pr_gate(item, 'verification'),
                )

    def test_pls1395_unreadable_origin_pr_is_retryable_not_stale_open(self):
        item = self._item(prs='open')
        with mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}, clear=False):
            with mock.patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                return_value=(False, {}),
            ):
                with self.assertRaises(WorkflowValidationError) as ctx:
                    WorkflowService._validate_verification_pr_gate(item, 'verification')
        msg = str(ctx.exception)
        self.assertIn('retryable', msg)
        self.assertIn('PLS-1395', msg)
        self.assertNotIn('not merged', msg)

    def test_validate_transition_enforces_verification_pr_gate(self):
        # The gate is wired into the global validate_transition path.
        item = self._item(prs='open', status='in_review')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.validate_transition(
                item, 'verification', self._actor(),
            )
        self.assertIn('verification', str(ctx.exception))

    # ── terminal set includes closed ─────────────────────────────────────────

    def test_closed_is_gated_terminal(self):
        item = self._item(acceptance_mode='deployed', prs='merged')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'closed')
        msg = str(ctx.exception)
        self.assertIn('missing_correlated_deploy_event', msg)
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_target'], 'closed')
        self.assertEqual(liveness['last_result'], 'block')
        self.assertIn(
            'missing_correlated_deploy_event', liveness['missing_classes'],
        )

    # ── free-form comments are NEVER authority ───────────────────────────────

    def test_freeform_probe_comment_does_not_pass(self):
        item = self._item(acceptance_mode='live', labels=['service-affecting'])
        self._comment(
            item,
            'deploy-verified: curl https://prod/x → 200, fix confirmed live',
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_freeform_waiver_comment_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._comment(
            item,
            'deploy-verify: waived — config-only revert, nothing to deploy',
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    # ── uncorrelated events do not pass ──────────────────────────────────────

    def test_wrong_sha_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, commit_sha='ffffffffffffffffffffffffffffffffffffffff')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_wrong_repo_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, repo='other-org/other-repo')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_staging_env_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, environment='staging')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_wrong_task_key_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, source_task_keys=['PLAT-99999'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_non_success_state_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, state='rollout_started', smoke_result='passed')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_failed_smoke_does_not_pass(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, state='smoke_failed', smoke_result='failed')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    # ── partial correlated evidence names missing classes ────────────────────

    def test_missing_image_named(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, image='', image_digest='', image_tag='')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_image_or_digest', str(ctx.exception))

    def test_missing_replicas_ready_named(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item, replicas_ready=False)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_replicas_ready', str(ctx.exception))

    def test_live_missing_probe_named(self):
        item = self._item(acceptance_mode='live')
        self._deploy_event(item, live_probe=False)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    def test_merged_head_without_sha_named(self):
        # merged_at set but no head_sha → cannot correlate.
        item = self._item(
            acceptance_mode='deployed',
            prs=[{
                'url': f'https://origin.shizuha.com/{REPO}/pulls/1',
                'repo': REPO,
                'status': 'merged',
                'merged_at': '2026-07-03T00:00:00Z',
            }],
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_merged_head_sha', str(ctx.exception))

    # ── full correlated evidence passes ──────────────────────────────────────

    def test_exact_correlated_event_passes_deployed(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(item)
        self.record_heartbeat.reset_mock()
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.record_heartbeat.assert_called_once_with('deploy_verify_gate')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_event')
        self.assertEqual(liveness['acceptance_mode'], 'deployed')
        self.assertEqual(liveness['missing_classes'], [])
        self.assertEqual(liveness['last_heartbeat_ts'], 1700000000.0)

    def test_exact_merge_commit_event_passes_deployed(self):
        merge_sha = 'f' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=merge_sha)
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_prefers_verified_by_qa_event_over_newer_smoke_passed(self):
        """PLS-1112 / PLS-1080: when a complete verified_by_qa event and an
        incomplete smoke_passed event both correlate, the gate must pick the
        verified_by_qa one even when the smoke_passed row is newer by
        updated_at. Returning the newest matching event blindly rejected a
        satisfied live task (missing_image_or_digest / missing_replicas_ready /
        missing_live_probe_or_qa)."""
        item = self._item(acceptance_mode='live')
        # Complete verified_by_qa event (image + replicas + QA verdict) — OLDER.
        complete = self._deploy_event(
            item, state='verified_by_qa', image='registry.example/x:abc',
            replicas_ready=True,
        )
        # Incomplete smoke_passed event (no image, no replicas) — NEWER.
        incomplete = self._deploy_event(
            item, state='smoke_passed', image='', replicas_ready=False,
        )
        from django.utils import timezone
        from datetime import timedelta
        DeploymentEvent = complete.__class__
        DeploymentEvent.objects.filter(pk=complete.pk).update(
            updated_at=timezone.now() - timedelta(hours=2),
        )
        DeploymentEvent.objects.filter(pk=incomplete.pk).update(
            updated_at=timezone.now(),
        )
        ev = WorkflowService._find_correlated_deploy_event(
            item, WorkflowService._merged_pr_heads(item),
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.pk, complete.pk)
        self.assertEqual((ev.state or '').lower(), 'verified_by_qa')
        # The live gate must pass with the complete event selected.
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_event')
        self.assertEqual(liveness['missing_classes'], [])

    def test_unrelated_commit_does_not_match_merge_or_head(self):
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(
                sha=MERGED_SHA,
                merge_sha='f' * 40,
            )],
        )
        self._deploy_event(item, commit_sha='e' * 40)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def _compare_response(self, status='ahead', behind_by=0, ahead_by=3):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            'status': status, 'behind_by': behind_by, 'ahead_by': ahead_by,
        }
        return resp

    def test_descendant_default_branch_event_passes_when_ancestry_proven(self):
        # PLS-904 / PLAT-4573: a truthful DeploymentEvent at a later
        # default-branch commit that CONTAINS the merge commit must satisfy the
        # deploy verification when Origin proves the ancestry — WITHOUT
        # rewriting the event to the feature head or merge commit.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)
        with mock.patch('requests.get', return_value=self._compare_response()) as rget, \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        # The ancestry proof must hit the CANONICAL Origin repo for the exact
        # merged-head ... deployed-commit pair.
        called = rget.call_args[0][0]
        self.assertIn(f'compare/{merge_sha}...{deployed_sha}', called)
        self.assertIn('shizuha-labs/x', called)

    def test_descendant_event_rejected_when_non_ancestor(self):
        # 'diverging' => the deployed commit does NOT contain the merge commit;
        # ancestry is refuted, so the event must NOT correlate.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)
        with mock.patch('requests.get', return_value=self._compare_response(
                status='diverging', behind_by=2, ahead_by=2)), \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_descendant_event_rejected_on_origin_fetch_failure(self):
        # A network/API failure is NEVER deploy evidence — the gate fails
        # closed rather than granting correlation on an unprovable ancestry.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)
        with mock.patch('requests.get', side_effect=OSError('origin down')), \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    @staticmethod
    def _forgejo_compare_response(total_commits):
        # Origin Forgejo/Gitea compare shape: only {commits, files,
        # total_commits} — no ``status``/``behind_by``/``ahead_by`` fields
        # (live-observed on origin.shizuha.com, 2026-09-21).
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            'commits': [], 'files': [], 'total_commits': total_commits,
        }
        return resp

    # Deterministically inert answer for the PLS-1249 seam: a non-CI filename
    # makes pr_files_are_ci_only() return False, so the CI-only close class
    # never engages and the gate reaches the descendant compare calls.
    _NON_CI_FILES_RESPONSE = [
        {'filename': 'tasks/services/workflow_service.py'},
    ]

    @staticmethod
    def _inert_non_compare_response(url, headers=None, timeout=None):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = (
            {'state': 'success'} if '/commits/' in url and url.endswith('/status')
            else list(_NON_CI_FILES_RESPONSE)
        )
        return resp

    def test_descendant_ancestry_proven_on_forgejo_compare_shape(self):
        # PLS-904 Forgejo contract: with no ``status`` field, containment is
        # proven via the SWAPPED pair (deployed as compare base) — its
        # total_commits must be 0 for the merged head to be contained. This is
        # the shape every Origin Forgejo compare actually returns, so the
        # descendant clause must work here and not only on GitHub-shaped APIs.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)
        compare_urls = []

        def rget(url, headers=None, timeout=None):
            if '/compare/' in url:
                compare_urls.append(url)
                return self._forgejo_compare_response(
                    0 if url.split('/compare/')[-1].startswith(deployed_sha)
                    else 1612
                )
            return self._inert_non_compare_response(url)

        with mock.patch('requests.get', side_effect=rget), \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertTrue(compare_urls)
        self.assertIn(f'compare/{merge_sha}...{deployed_sha}', compare_urls[0])
        self.assertIn(f'compare/{deployed_sha}...{merge_sha}', compare_urls[1])

    def test_descendant_ancestry_refuted_on_forgejo_shape_diverged(self):
        # Swapped total_commits > 0 means the merged head is NOT contained in
        # the deployed commit (empirical negative control: un-rebased open-PR
        # head vs main tip = 143) — never deploy evidence.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)

        def rget(url, headers=None, timeout=None):
            if '/compare/' in url:
                return self._forgejo_compare_response(
                    143 if url.split('/compare/')[-1].startswith(deployed_sha)
                    else 3
                )
            return self._inert_non_compare_response(url)

        with mock.patch('requests.get', side_effect=rget), \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_descendant_ancestry_fails_closed_on_forgejo_shape_malformed(self):
        # A Forgejo response without a parseable total_commits grants nothing.
        merge_sha = 'f' * 40
        deployed_sha = 'd' * 40
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA, merge_sha=merge_sha)],
        )
        self._deploy_event(item, commit_sha=deployed_sha)

        def malformed_response(url, headers=None, timeout=None):
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {'commits': [], 'files': []}
            return resp

        with mock.patch('requests.get', side_effect=malformed_response), \
                mock.patch.dict('os.environ', {
                    'ORIGIN_URL': 'https://origin.shizuha.com',
                    'FORGEJO_TOKEN': 'test-token',
                }):
            self.assertFalse(
                WorkflowService._prove_deployed_descends_from_merged_head(
                    'shizuha-labs', 'x', merge_sha, deployed_sha,
                )
            )

    def test_ancestry_helper_fails_closed_on_short_or_empty_shas(self):
        # Full-SHA discipline: short/empty SHAs never reach the wire and never
        # grant ancestry (exact-match discipline preserved).
        self.assertFalse(WorkflowService._prove_deployed_descends_from_merged_head(
            'shizuha-labs', 'x', MERGED_SHA_SHORT, 'd' * 40))
        self.assertFalse(WorkflowService._prove_deployed_descends_from_merged_head(
            'shizuha-labs', 'x', MERGED_SHA, ''))
        self.assertFalse(WorkflowService._prove_deployed_descends_from_merged_head(
            '', '', MERGED_SHA, 'd' * 40))
        # Exact identity of base and deployed is ancestry-trivial (no network).
        self.assertTrue(WorkflowService._prove_deployed_descends_from_merged_head(
            'shizuha-labs', 'x', MERGED_SHA, MERGED_SHA))

    def test_short_sha_prefix_does_not_match(self):
        # Rei P1-4: exact full-SHA correlation required. A short prefix is not
        # proof of correlation and must NOT pass.
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(sha=MERGED_SHA_SHORT)],
        )
        self._deploy_event(item, commit_sha=MERGED_SHA)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_cross_org_same_basename_does_not_match(self):
        # Rei P1-4: full owner/repo required. A same-basename repo in a
        # DIFFERENT org must NOT correlate.
        item = self._item(
            acceptance_mode='deployed',
            prs=[self._merged_pr(repo='shizuha-labs/x')],
        )
        self._deploy_event(item, repo='other-org/x')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_replicas_ready_via_ready_desired_counts(self):
        item = self._item(acceptance_mode='deployed')
        self._deploy_event(
            item,
            replicas_ready={
                'ready_replicas': 3,
                'desired_replicas': 3,
            },
        )
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_live_with_event_verified_by_qa_passes(self):
        # First-class deploy-pipeline state verified_by_qa is the only
        # event-level live-probe signal (Rei P1-3).
        item = self._item(acceptance_mode='live')
        self._deploy_event(item, state='verified_by_qa', live_probe=False)
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_live_event_metadata_probe_is_not_trusted(self):
        # Rei P1-3: event metadata.live_probe (even schema-tagged) has no
        # authenticated QA provenance and must NOT pass on its own.
        item = self._item(acceptance_mode='live')
        self._deploy_event(item, live_probe=True)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    def test_live_with_item_typed_probe_passes(self):
        # Rei P1: only an immutable mechanical live-probe receipt passes.
        item = self._item(acceptance_mode='live')
        self._deploy_event(item, live_probe=False)
        self._live_probe_receipt(item)
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_live_metadata_probe_claim_is_forgeable_and_not_trusted(self):
        # Client-writable metadata.live_probe is never authority.
        item = self._item(
            acceptance_mode='live',
            metadata={
                'live_probe': {
                    'schema': 'shizuha.pulse.live-probe.v1',
                    'result': 'passed',
                    'evidence': 'GET /health 200 on prod',
                    'verified_by': 'qa-lead@shizuha.com',
                },
            },
        )
        self._deploy_event(item, live_probe=False)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    def test_ordinary_live_probe_comment_is_not_authority(self):
        # Rei P1: ordinary mutable comment text is never live-probe authority.
        item = self._item(acceptance_mode='live')
        self._deploy_event(item, live_probe=False)
        self._comment(item, 'live probe: passed — GET /health 200 on prod',
                      email='qa-lead@shizuha.com')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    def test_live_probe_receipt_requires_qa_at_mint(self):
        item = self._item(acceptance_mode='live')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService.issue_live_probe_receipt(
                    item, actor=self._actor('jun@shizuha.com'),
                    event_id='test-live-probe-denied',
                )

    def test_service_affecting_default_live_requires_probe(self):
        item = self._item(labels=['service-affecting'])
        self._deploy_event(item, live_probe=False)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))
        self.assertIn('acceptance_mode=live', str(ctx.exception))

    def test_incident_workflow_defaults_live(self):
        item = self._item(labels=[], workflow_name='incident')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'resolved')
        self.assertIn('acceptance_mode=live', str(ctx.exception))

    # ── role-gated durable waiver (Comment provenance) ───────────────────────

    def test_role_gated_waiver_passes_deployed(self):
        # Rei P1: only an immutable mechanical waiver receipt passes.
        item = self._item(acceptance_mode='deployed')
        self._waiver_receipt(item)
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_waiver')

    def test_metadata_waiver_claim_is_forgeable_and_not_trusted(self):
        item = self._item(
            acceptance_mode='deployed',
            metadata={'deploy_verify_waiver': self._waiver()},
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_ordinary_waiver_comment_is_not_authority(self):
        # Rei P1: ordinary mutable comment text is never waiver authority.
        item = self._item(acceptance_mode='deployed')
        self._comment(item, 'deploy verify waiver: issued — config-only',
                      email='devops@shizuha.com')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_automation_cannot_mint_waiver_receipt(self):
        item = self._item(acceptance_mode='deployed')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService.issue_deploy_verify_waiver(
                    item, actor=self._actor('system@shizuha.com'),
                    reason='nope', event_id='test-waiver-auto',
                    evidence={
                        'kind': 'inline_package',
                        'actor': 'test',
                        'originating_key': 'test',
                        'reason': 'nope',
                        'target_head': 'test',
                        'verdict_refs': ['test'],
                        'scope_conditions': ['test'],
                    },
                )

    def test_waiver_receipt_requires_role_at_mint(self):
        item = self._item(acceptance_mode='deployed')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService.issue_deploy_verify_waiver(
                    item, actor=self._actor('random@example.com'),
                    reason='nope', event_id='test-waiver-denied',
                    evidence={
                        'kind': 'inline_package',
                        'actor': 'test',
                        'originating_key': 'test',
                        'reason': 'nope',
                        'target_head': 'test',
                        'verdict_refs': ['test'],
                        'scope_conditions': ['test'],
                    },
                )

    def test_operator_can_mint_waiver_receipt_without_team_role(self):
        # PLS-675 operator waiver authority: the human operator (CEO) may issue
        # a deploy-verify waiver for work they have personally verified live,
        # even without devops/admin-ops/qa membership. Never automation.
        item = self._item(acceptance_mode='deployed')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ):
            receipt = WorkflowService.issue_deploy_verify_waiver(
                item, actor=self._actor('operator@example.test'),
                reason='operator live-verified the deployed surface as hritik',
                event_id='test-waiver-operator',
                    evidence={
                        'kind': 'inline_package',
                        'actor': 'test',
                        'originating_key': 'test',
                        'reason': 'nope',
                        'target_head': 'test',
                        'verdict_refs': ['test'],
                        'scope_conditions': ['test'],
                    },
            )
        payload = receipt.origin_payload
        self.assertEqual(payload['issuer_team'], 'operator')
        self.assertEqual(payload['issuer_email'], 'operator@example.test')
        self.assertEqual(payload['result'], 'issued')

    def test_operator_waiver_passes_gate(self):
        # An operator-issued immutable waiver receipt satisfies the gate.
        item = self._item(acceptance_mode='live')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ):
            WorkflowService.issue_deploy_verify_waiver(
                item, actor=self._actor('operator@example.test'),
                reason='operator live-verified the deployed surface as hritik',
                event_id='test-waiver-operator-gate',
                    evidence={
                        'kind': 'inline_package',
                        'actor': 'test',
                        'originating_key': 'test',
                        'reason': 'nope',
                        'target_head': 'test',
                        'verdict_refs': ['test'],
                        'scope_conditions': ['test'],
                    },
            )
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_waiver')

    def test_non_operator_without_role_cannot_mint_waiver(self):
        # A non-operator, non-devops/qa actor is still refused at mint.
        item = self._item(acceptance_mode='deployed')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService.issue_deploy_verify_waiver(
                    item, actor=self._actor('jun@shizuha.com'),
                    reason='nope', event_id='test-waiver-jun-denied',
                    evidence={
                        'kind': 'inline_package',
                        'actor': 'test',
                        'originating_key': 'test',
                        'reason': 'nope',
                        'target_head': 'test',
                        'verdict_refs': ['test'],
                        'scope_conditions': ['test'],
                    },
                )

    # ── block audit / liveness ───────────────────────────────────────────────

    def test_block_records_liveness_and_mechanical_comment(self):
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.record_heartbeat.assert_called_once_with('deploy_verify_gate')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'block')
        self.assertEqual(liveness['acceptance_mode'], 'deployed')
        self.assertIn(
            'missing_correlated_deploy_event', liveness['missing_classes'],
        )
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                author_email='workflow@pulse',
                origin_event='validator.deploy_verify_blocked',
            ).exists()
            or Comment.objects.filter(
                item=item,
                content__contains='metric:deploy_verify_gate_blocked PLAT-1351',
            ).exists()
        )

    # ── merge-event transition (Rei P1-1 / P1-2) ─────────────────────────────

    def test_apply_merge_event_transition_skips_non_awaiting_merge(self):
        item = self._item(acceptance_mode='deployed', status='in_review')
        result = WorkflowService.apply_merge_event_transition(item)
        self.assertIsNone(result)

    def test_apply_merge_event_transition_governed_non_terminal(self):
        # Rei P1-1: a merged deployed/live task at awaiting_merge must move to a
        # non-terminal deploy stage, never terminalize directly.
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with mock.patch.object(
            WorkflowService, '_verify_live_pr_merge_state',
            return_value=('proceed', 'test-stub'),
        ), mock.patch.object(
            WorkflowService, 'execute_transition', return_value=item,
        ) as exec_mock:
            target = WorkflowService.apply_merge_event_transition(
                item, pr_html_url='https://origin.shizuha.com/x/pulls/1',
            )
        self.assertEqual(target, 'awaiting_deploy')
        exec_mock.assert_called_once()
        kwargs = exec_mock.call_args.kwargs
        self.assertEqual(kwargs.get('new_status'), 'awaiting_deploy')
        self.assertTrue(
            Comment.objects.filter(
                item=item, content__contains='Auto-transitioned to `awaiting_deploy`',
            ).exists()
        )

    def test_apply_merge_event_transition_failure_does_not_write_raw_comment(self):
        # PLAT-6012: retry orchestration owns one typed failure receipt. The
        # helper must not append a raw comment on every caller attempt.
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition',
            side_effect=WorkflowValidationError('boom'),
        ):
            target = WorkflowService.apply_merge_event_transition(item)
        self.assertIsNone(target)
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains='merge-event auto-transition FAILED',
            ).exists()
        )

    # ── PLAT-7047: pre-action live-state re-verify (502-replay fix) ──────────

    def _guard_env(self):
        return mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'guard-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        })

    @staticmethod
    def _guard_response(payload):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_verify_live_pr_merge_state_proceeds_on_live_merged(self):
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        payload = {
            'state': 'closed', 'merged': True,
            'merged_at': '2026-09-04T12:00:00Z',
            'head': {'sha': MERGED_SHA},
        }
        with self._guard_env(), mock.patch(
            'requests.get', return_value=self._guard_response(payload),
        ) as get_mock:
            verdict, detail = WorkflowService._verify_live_pr_merge_state(
                item, f'https://origin.shizuha.com/{REPO}/pulls/1',
            )
        self.assertEqual(verdict, 'proceed')
        self.assertIn('live_merged=true', detail)
        self.assertIn(f'{REPO}/pulls/1', get_mock.call_args.args[0])

    def test_verify_live_pr_merge_state_skips_closed_unmerged(self):
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        payload = {'state': 'closed', 'merged': False, 'head': {'sha': MERGED_SHA}}
        with self._guard_env(), mock.patch(
            'requests.get', return_value=self._guard_response(payload),
        ):
            verdict, detail = WorkflowService._verify_live_pr_merge_state(
                item, f'https://origin.shizuha.com/{REPO}/pulls/1',
            )
        self.assertEqual(verdict, 'skip_not_merged')
        self.assertIn('live_state=closed', detail)

    def test_verify_live_pr_merge_state_skips_head_mismatch(self):
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        payload = {
            'state': 'closed', 'merged': True,
            'head': {'sha': 'f' * 40},  # live head != cited MERGED_SHA
        }
        with self._guard_env(), mock.patch(
            'requests.get', return_value=self._guard_response(payload),
        ):
            verdict, detail = WorkflowService._verify_live_pr_merge_state(
                item, f'https://origin.shizuha.com/{REPO}/pulls/1',
            )
        self.assertEqual(verdict, 'skip_head_mismatch')
        # The gate's detail string truncates both head SHAs to 10 chars
        # (cited={cited_head[:10]} live={live_head[:10]}) — the compact
        # display convention main standardized; assert the emitted form.
        self.assertIn('cited=deadbeefca', detail)
        self.assertIn('live=ffffffffff', detail)

    def test_verify_live_pr_merge_state_unavailable_on_outage(self):
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with self._guard_env(), mock.patch(
            'requests.get', side_effect=ConnectionError('origin 502'),
        ):
            verdict, detail = WorkflowService._verify_live_pr_merge_state(
                item, f'https://origin.shizuha.com/{REPO}/pulls/1',
            )
        self.assertEqual(verdict, 'unavailable')
        self.assertIn('live_query_failed', detail)

    def test_apply_merge_event_transition_skips_and_comments_on_guard_skip(self):
        # The 502-replay hazard: a queued merge event for a PR the live API
        # reports closed-unmerged must NOT fire the post-merge transition.
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with mock.patch.object(
            WorkflowService, '_verify_live_pr_merge_state',
            return_value=('skip_not_merged', 'live_state=closed'),
        ), mock.patch.object(
            WorkflowService, 'execute_transition', return_value=item,
        ) as exec_mock:
            target = WorkflowService.apply_merge_event_transition(
                item, pr_html_url=f'https://origin.shizuha.com/{REPO}/pulls/1',
            )
        self.assertIsNone(target)
        exec_mock.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        # PLAT-7047: the skip notice is a mechanical comment minted by
        # MechanicalCommentFactory (identity 'workflow@pulse'), not a direct
        # system-authored Comment.objects.create.
        skipped = Comment.objects.filter(
            item=item,
            author_email='workflow@pulse',
            origin_event='merge_event.skip_live_state',
            content__contains='PLAT-7047 merge-event SKIPPED (skip_not_merged)',
        )
        self.assertTrue(skipped.exists())

    def test_security_merge_event_uses_real_transition_and_holds_cleanly(self):
        # Production boundary: security findings target the configured
        # verification edge. A non-transient evidence HOLD stays awaiting_merge
        # and does not fall back to the invalid generic `done` status.
        self._seed_security_merge_workflow()
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='merged',
            workflow_name='security-finding',
        )
        target = WorkflowService.apply_merge_event_transition(item)
        item.refresh_from_db()
        self.assertIsNone(target)
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains="Invalid status 'done'",
            ).exists()
        )

    def test_merge_event_retry_backoff_and_comment_dedupe_production_order(self):
        # PLAT-6012/PLAT-6027: run the real top-level retry sequence twice. A
        # stable HOLD writes one comment, arms 5m backoff, and the second sweep
        # neither calls the transition nor appends another comment.
        #
        # PLAT-7066 (Retro 27Aug): a live security-finding with NO frozen
        # VerificationRequirementManifest hits requirement_manifest_invalid —
        # a Security-side issue, NOT a merge action. The merge lane must NOT
        # arm retry backoff; it records the pending marker, routes to Security,
        # and the second sweep early-returns without calling the transition.
        import os
        from tasks.tasks import sync_linked_pr_statuses

        self._seed_security_merge_workflow()
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='merged',
            workflow_name='security-finding',
        )
        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=False), \
             mock.patch('tasks.tasks.send_alert_notification.delay'), \
             mock.patch(
                'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                return_value=(mock.Mock(id=99), True),
             ), \
             mock.patch.object(
                WorkflowService, '_verify_live_pr_merge_state',
                return_value=('proceed', 'test-stub'),
             ), \
             mock.patch.object(
                WorkflowService, 'apply_merge_event_transition',
                wraps=WorkflowService.apply_merge_event_transition,
             ) as apply_mock:
            sync_linked_pr_statuses.run(limit=1)
            sync_linked_pr_statuses.run(limit=1)

        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertEqual(apply_mock.call_count, 1)
        # PLAT-7066: routed to Security, NOT armed for merge-lane retry.
        self.assertTrue(
            item.metadata.get('merge_event_requirement_manifest_pending')
        )
        self.assertNotIn('merge_event_retry_attempts', item.metadata)
        self.assertNotIn('merge_event_retry_next_attempt_at', item.metadata)
        self.assertTrue(
            Comment.objects.filter(
                item=item, content__contains='ROUTED TO SECURITY',
            ).exists()
        )


    def test_apply_merge_event_transition_fail_loud_is_idempotent(self):
        # PLAT-6012: the helper no longer writes a raw failure comment — the
        # retry lane owns the single typed/deduped receipt. Repeated caller
        # attempts must not append any comment (PLAT-250 write storm).
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition',
            side_effect=WorkflowValidationError('boom'),
        ):
            WorkflowService.apply_merge_event_transition(item)
            WorkflowService.apply_merge_event_transition(item)
        self.assertEqual(
            Comment.objects.filter(
                item=item, content__contains='merge-event auto-transition FAILED',
            ).count(),
            0,
        )

    def test_attempt_merge_event_retry_skips_open_linked_pr(self):
        # Production caller: sync_linked_pr_statuses dedicated lane on a
        # recurring task with historical merged PRs + one still-open PR
        # (PLAT-250). Must not apply or comment.
        from tasks.tasks import _attempt_merge_event_retry

        item = self._item(
            acceptance_mode='code',
            status='awaiting_merge',
            prs=[
                self._merged_pr(),
                self._merged_pr(repo='shizuha-labs/y'),
                self._open_pr(),
            ],
        )
        before = Comment.objects.filter(item=item).count()
        with mock.patch.object(
            WorkflowService, 'apply_merge_event_transition',
        ) as apply_mock:
            self.assertFalse(_attempt_merge_event_retry(item))
            apply_mock.assert_not_called()
        self.assertEqual(Comment.objects.filter(item=item).count(), before)

    def test_attempt_merge_event_retry_one_apply_per_sweep(self):
        # Production caller: _attempt_merge_event_retry from the dedicated
        # lane. Two consecutive top-level attempts on an item with many
        # merged PRs must apply once per attempt and write one fail-loud
        # comment total (not one per PR per sweep).
        from tasks.tasks import _attempt_merge_event_retry

        item = self._item(
            acceptance_mode='code',
            status='awaiting_merge',
            prs=[
                self._merged_pr(),
                self._merged_pr(repo='shizuha-labs/y'),
                self._merged_pr(repo='shizuha-labs/z'),
            ],
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition',
            side_effect=WorkflowValidationError('boom'),
        ), mock.patch(
            'tasks.tasks.send_alert_notification.delay',
        ), mock.patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
            return_value=(mock.Mock(id=99), False),
        ), mock.patch(
            'tasks.liveness.record_merge_event_retry_failure',
        ):
            self.assertFalse(_attempt_merge_event_retry(item))
            self.assertFalse(_attempt_merge_event_retry(item))
        self.assertEqual(
            Comment.objects.filter(
                item=item, content__contains='PLS-675 merge-event retry PENDING',
            ).count(),
            1,
        )

    def test_security_merge_event_uses_real_transition_and_holds_cleanly(self):
        # Production boundary: security findings target the configured
        # verification edge. A non-transient evidence HOLD stays awaiting_merge
        # and does not fall back to the invalid generic `done` status.
        self._seed_security_merge_workflow()
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='merged',
            workflow_name='security-finding',
        )
        target = WorkflowService.apply_merge_event_transition(item)
        item.refresh_from_db()
        self.assertIsNone(target)
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains="Invalid status 'done'",
            ).exists()
        )

    def test_origin_reconciler_two_sweep_production_order(self):
        # Rei P2: REAL Origin reconciler twice in production order.
        # Fixture mirrors the live caller sequence:
        #   1) stored PR is open; provider confirms open→merged
        #   2) first real workflow transition fails at the persistence boundary
        #   3) second top-level sweep retries through the actual workflow once
        #   4) early terminal close refused; correlated deploy then permits close
        # Do NOT seed metadata as already-merged, and do NOT replace
        # execute_transition with a status-writing stub.
        import os
        from django.db import DatabaseError
        from tasks.tasks import sync_linked_pr_statuses
        from tasks.liveness import read_merge_event_retry_failure
        from tasks.services.mechanical_comments import MechanicalCommentFactory

        isolation = ExitStack()
        self.addCleanup(isolation.close)
        isolation.enter_context(_patch_isolated_liveness(db=14))
        self._seed_governed_merge_workflow('autonomous-bug')
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_merge',
            prs='open',
            workflow_name='autonomous-bug',
        )
        # GitHub-shaped open PR so the open-lane provider path is exercised.
        md = dict(item.metadata or {})
        md['pull_requests'] = [{
            'url': f'https://github.com/{REPO}/pull/1',
            'repo': REPO,
            'number': 1,
            'status': 'open',
            'head_sha': MERGED_SHA,
        }]
        item.metadata = md
        item.workflow_name = 'autonomous-bug'
        item.workflow_status = 'awaiting_merge'
        item.save(update_fields=['metadata', 'workflow_name', 'workflow_status'])

        class _MergedResp:
            status_code = 200
            ok = True

            def json(self):
                return {
                    'state': 'closed',
                    'merged': True,
                    'merged_at': '2026-07-03T00:00:00Z',
                    'merged_by': {'login': 'kai2574'},
                    'html_url': f'https://github.com/{REPO}/pull/1',
                    'head': {'sha': MERGED_SHA},
                    # PLS-521: provider merge attestation requires full merge SHA
                    'merge_commit_sha': MERGED_SHA,
                    'user': {'login': 'author'},
                }

        def _api_request(url, **kwargs):
            return ('github', 'shizuha-labs', 'x', 1, _MergedResp())

        # Fail the first status persistence of awaiting_deploy only; subsequent
        # saves (and the second sweep's real transition) succeed.
        real_save = Item.save
        persist_failures = {'n': 0}

        def _save_with_one_persist_failure(self, *args, **kwargs):
            target = (getattr(self, 'status', '') or '').strip().lower()
            update_fields = kwargs.get('update_fields')
            status_write = (
                update_fields is None
                or (update_fields and 'status' in update_fields)
            )
            if (
                target == 'awaiting_deploy'
                and status_write
                and persist_failures['n'] == 0
            ):
                persist_failures['n'] += 1
                raise DatabaseError('transient status persistence failure')
            return real_save(self, *args, **kwargs)

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=False), \
             mock.patch('tasks.tasks._linked_pr_api_request', side_effect=_api_request), \
             mock.patch.object(Item, 'save', _save_with_one_persist_failure), \
             mock.patch('tasks.tasks.send_alert_notification.delay'), \
             mock.patch(
                'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                return_value=(mock.Mock(id=99), True),
             ):
            # Sweep 1 — provider confirms merge; first real transition fails at
            # persistence; item stays awaiting_merge with fail-loud evidence.
            sync_linked_pr_statuses.run(limit=50)
            item.refresh_from_db()
            self.assertEqual(item.status, 'awaiting_merge')
            prs = (item.metadata or {}).get('pull_requests') or []
            self.assertTrue(prs and (prs[0].get('status') or '').lower() == 'merged')
            self.assertEqual(persist_failures['n'], 1)
            self.assertTrue(
                Comment.objects.filter(
                    item=item,
                    origin_event='receipt.merge_event_retry_pending',
                    author_email='workflow@pulse',
                ).exists(),
                'typed pending receipt must be on the task after first failure',
            )
            # The write (via _report_merge_event_retry_pending ->
            # record_merge_event_retry_failure) goes through _redis_client(),
            # which _patch_isolated_liveness(db=14) routes to the isolated
            # store. Read back through the same _redis_client() so the
            # assertion sees the recorded failure (client=self._redis would
            # read the setUp fake, which never receives the write).
            failure = read_merge_event_retry_failure()
            self.assertIsNotNone(failure)
            self.assertEqual(failure.get('state'), 'pending')
            self.assertEqual(failure.get('item_key'), item.item_key)

            # Sweep 2 — dedicated retry lane runs the REAL workflow transition
            # and advances exactly once to awaiting_deploy.
            md = dict(item.metadata or {})
            md['merge_event_retry_next_attempt_at'] = '2000-01-01T00:00:00+00:00'
            item.metadata = md
            item.save(update_fields=['metadata'])
            sync_linked_pr_statuses.run(limit=50)
            item.refresh_from_db()
            self.assertEqual(item.status, 'awaiting_deploy')

        # Early terminal close refused without deploy evidence.
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

        # Correlated deploy evidence permits close.
        self._deploy_event(item)
        WorkflowService._validate_deploy_verify_gate(item, 'done')

        # Direct factory mint with forged issuer email must not qualify.
        forged_actor = self._actor('forger@evil.example')
        with self.assertRaises(Exception):
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.live_probe_passed',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'passed',
                    'issuer_email': 'qa-lead@shizuha.com',  # forged
                    'issuer_team': 'qa',
                    'issued_at': '2026-08-01T00:00:00Z',
                    'deployment_event_id': '00000000-0000-0000-0000-000000000001',
                    'deployment_repo': REPO,
                    'deployment_commit_sha': MERGED_SHA,
                    'deployment_image': 'registry.example/x:abc',
                    'deployment_image_digest': '',
                    'deployment_image_tag': '',
                    'deployment_replicas_ready': True,
                    'deployment_converged_at': '2026-08-01T00:00:00Z',
                    'evidence': 'forged',
                },
                origin_event_id=f'forged-live-{item.item_key}',
                causal_actor=forged_actor,
            )

        # Same-email + forged team claim must also be refused (Rei P1).
        with self.assertRaises(Exception):
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.live_probe_passed',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'passed',
                    'issuer_email': 'forger@evil.example',
                    'issuer_team': 'qa',  # claimed without membership
                    'issued_at': '2026-08-01T00:00:00Z',
                    'deployment_event_id': '00000000-0000-0000-0000-000000000001',
                    'deployment_repo': REPO,
                    'deployment_commit_sha': MERGED_SHA,
                    'deployment_image': 'registry.example/x:abc',
                    'deployment_image_digest': '',
                    'deployment_image_tag': '',
                    'deployment_replicas_ready': True,
                    'deployment_converged_at': '2026-08-01T00:00:00Z',
                    'evidence': 'forged-team',
                },
                origin_event_id=f'forged-team-live-{item.item_key}',
                causal_actor=forged_actor,
            )

    def test_receipt_issuer_bound_to_causal_actor(self):
        # Rei P1: production write path (issue_live_probe_receipt) binds issuer
        # to authenticated causal actor; forged factory mint is refused; gate
        # rejects receipts whose payload issuer != caused_by snapshot.
        from tasks.services.mechanical_comments import (
            MechanicalCommentError,
            MechanicalCommentFactory,
        )

        item = self._item(acceptance_mode='live', prs='merged')
        actor = self._actor('qa-api@shizuha.com')
        # PLS-675 / rui P1: mint only after correlated deploy exists.
        deploy = self._deploy_event(item, live_probe=False)

        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            receipt = WorkflowService.issue_live_probe_receipt(
                item, actor=actor, evidence='GET /health 200',
                event_id=f'api-live-{item.item_key}',
                deployment_event=deploy,
            )
        self.assertEqual(receipt.caused_by_email_snapshot, 'qa-api@shizuha.com')
        self.assertEqual(
            receipt.origin_payload['issuer_email'], 'qa-api@shizuha.com',
        )
        self.assertEqual(
            str(receipt.origin_payload['deployment_event_id']), str(deploy.pk),
        )
        self.assertEqual(
            receipt.origin_payload['deployment_commit_sha'], MERGED_SHA,
        )

        # Forged issuer vs causal actor refused at mint.
        with self.assertRaises(MechanicalCommentError):
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.live_probe_passed',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'passed',
                    'issuer_email': 'qa-lead@shizuha.com',
                    'issuer_team': 'qa',
                    'issued_at': '2026-08-01T00:00:00Z',
                    'deployment_event_id': '00000000-0000-0000-0000-000000000001',
                    'deployment_repo': REPO,
                    'deployment_commit_sha': MERGED_SHA,
                    'deployment_image': 'registry.example/x:abc',
                    'deployment_image_digest': '',
                    'deployment_image_tag': '',
                    'deployment_replicas_ready': True,
                    'deployment_converged_at': '2026-08-01T00:00:00Z',
                    'evidence': 'forged',
                },
                origin_event_id=f'forged-live2-{item.item_key}',
                causal_actor=self._actor('forger@evil.example'),
            )

        # Missing causal actor refused.
        with self.assertRaises(MechanicalCommentError):
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.live_probe_passed',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'passed',
                    'issuer_email': 'qa-lead@shizuha.com',
                    'issuer_team': 'qa',
                    'issued_at': '2026-08-01T00:00:00Z',
                    'deployment_event_id': '00000000-0000-0000-0000-000000000001',
                    'deployment_repo': REPO,
                    'deployment_commit_sha': MERGED_SHA,
                    'deployment_image': 'registry.example/x:abc',
                    'deployment_image_digest': '',
                    'deployment_image_tag': '',
                    'deployment_replicas_ready': True,
                    'deployment_converged_at': '2026-08-01T00:00:00Z',
                    'evidence': 'no-actor',
                },
                origin_event_id=f'no-actor-{item.item_key}',
                causal_actor=None,
            )

        WorkflowService._validate_deploy_verify_gate(item, 'done')

        # Endpoint is registered on ItemViewSet (production write path surface).
        from tasks.views import ItemViewSet
        paths = {
            getattr(m, 'url_path', None)
            for m in ItemViewSet.get_extra_actions()
        }
        self.assertIn('issue-live-probe-receipt', paths)
        self.assertIn('issue-deploy-verify-waiver', paths)

    def test_pre_deploy_live_probe_receipt_does_not_satisfy_later_deployment(self):
        """rui P1: old-head receipt must not authorize a later deployment.

        Production order:
        1. Deploy head A converges; QA mints a receipt bound to deploy A.
        2. A newer deploy B (different head/image) becomes the correlated event.
        3. Terminal close must refuse — receipt is not bound to B.
        4. QA mints a post-deploy receipt bound to B → terminal close passes.
        """
        OLD_SHA = 'a' * 40
        NEW_SHA = MERGED_SHA  # matches default merged PR head
        item = self._item(acceptance_mode='live', prs='merged')

        # Deploy A at an old head (still correlated via task key + success, but
        # will lose correlation once B matches the merged head better... actually
        # _find_correlated_deploy_event requires sha match to merged heads.
        # So mint against A only works if A matches a merged head.
        # Sequence: start with only A matching MERGED_SHA, mint, then replace
        # with B also matching MERGED_SHA but different image/event id.
        deploy_a = self._deploy_event(
            item,
            live_probe=False,
            commit_sha=NEW_SHA,
            image='registry.example/x:old',
            image_digest='sha256:' + '1' * 64,
        )
        receipt_a = self._live_probe_receipt(item, deployment_event=deploy_a)
        self.assertEqual(
            str(receipt_a.origin_payload['deployment_event_id']), str(deploy_a.pk),
        )
        # Exact post-deploy receipt for A still passes while A is correlated.
        WorkflowService._validate_deploy_verify_gate(item, 'done')

        # Later deployment B (same merged head, new image/digest) becomes newest.
        deploy_b = self._deploy_event(
            item,
            live_probe=False,
            commit_sha=NEW_SHA,
            image='registry.example/x:new',
            image_digest='sha256:' + '2' * 64,
        )
        # Newest correlated event is B; receipt A must not authorize B.
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

        # Post-deploy QA receipt bound to B authorizes terminal close.
        self._live_probe_receipt(item, deployment_event=deploy_b)
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_live_probe_receipt_refuses_mint_before_deploy(self):
        """rui P1: cannot mint a live-probe receipt with no correlated deploy."""
        item = self._item(acceptance_mode='live', prs='merged')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService.issue_live_probe_receipt(
                    item, actor=self._actor('qa-lead@shizuha.com'),
                    event_id=f'pre-deploy-{item.item_key}',
                )
        # PLAT-9570: the message names the real rejecting dimension
        # (missing correlated event naming this task), not env.
        self.assertIn('correlated successful deployment '
                      'event naming this task', str(ctx.exception))

    def test_live_probe_mint_requires_replicas_ready_then_same_event_converges(self):
        """rui P1: pre-convergence mint refused; same-row converge then mint+close.

        Production order on one mutable DeploymentEvent UUID:
        1. smoke_passed + image but replicas_ready=false → mint refused
        2. same row upserted to replicas_ready=true → mint succeeds
        3. terminal close accepts the post-convergence receipt
        A receipt cannot be minted before convergence and later legitimized
        by the same ledger row flipping ready.
        """
        item = self._item(acceptance_mode='live', prs='merged')
        ev = self._deploy_event(item, live_probe=False, replicas_ready=False)
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService.issue_live_probe_receipt(
                    item, actor=self._actor('qa-lead@shizuha.com'),
                    deployment_event=ev,
                    event_id=f'pre-converge-{item.item_key}',
                )
        self.assertIn('replicas_ready', str(ctx.exception))

        # Same row converges (mutable ledger upsert semantics).
        meta = dict(ev.metadata or {})
        meta['replicas_ready'] = True
        ev.metadata = meta
        ev.save(update_fields=['metadata', 'updated_at'])
        ev.refresh_from_db()

        receipt = self._live_probe_receipt(item, deployment_event=ev)
        self.assertIs(receipt.origin_payload.get('deployment_replicas_ready'), True)
        self.assertEqual(
            str(receipt.origin_payload.get('deployment_event_id')), str(ev.pk),
        )
        WorkflowService._validate_deploy_verify_gate(item, 'done')


    def test_acceptance_mode_persists_on_update_fields_save(self):
        # Rei P1: create-time default must land even under update_fields saves.
        item = self._item(labels=['service-affecting'])
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'live')
        # Simulate a partial save that previously dropped the default.
        Item.objects.filter(pk=item.pk).update(acceptance_mode='')
        item.refresh_from_db()
        item.title = item.title + ' x'
        item.save(update_fields=['title'])
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'live')

    def test_fixture_orig221_open_pr_blocks_before_merge(self):
        # ORIG-221/ORIG-226: terminalized while authoritative PR still open.
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='open',
            labels=['service-affecting'],
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('open_linked_pr', str(ctx.exception))

    def test_fixture_kura120_merge_attempt_is_not_terminal(self):
        # KURA-120/KURA-123: merge-attempt must not terminalize; post-merge
        # lands non-terminal and still needs deploy evidence.
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='merged',
            labels=['service-affecting'],
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition', return_value=item,
        ):
            target = WorkflowService.apply_merge_event_transition(item)
        self.assertEqual(target, 'awaiting_deploy')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    # ── production-order regression (Rei P2-6) ───────────────────────────────

    def test_production_order_merge_then_gate_blocks_terminal(self):
        # PLAT-5074 / PLAT-4732 / DOJO-15 / SCLI-304 class: a merged deployed
        # task must NOT reach terminal directly from the merge event. After the
        # merge-event transition to awaiting_deploy, a terminal close still
        # requires deploy evidence.
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='merged',
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition', return_value=item,
        ):
            target = WorkflowService.apply_merge_event_transition(item)
        self.assertEqual(target, 'awaiting_deploy')
        # Now attempt terminal close — must be blocked (no deploy event).
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_production_order_merge_then_verified_close_passes(self):
        # Full production-order: merge → awaiting_deploy → correlated deploy
        # event → terminal close passes. Run twice (stateful recurrence).
        for _attempt in range(2):
            item = self._item(
                acceptance_mode='deployed', status='awaiting_merge', prs='merged',
            )
            with mock.patch.object(
                WorkflowService, 'execute_transition', return_value=item,
            ):
                target = WorkflowService.apply_merge_event_transition(item)
            self.assertEqual(target, 'awaiting_deploy')
            self._deploy_event(item)
            WorkflowService._validate_deploy_verify_gate(item, 'done')

    def test_fixture_plat5074_open_pr_blocks_terminal(self):
        # PLAT-5074 → PLAT-5226: terminalized while fixing PR still open.
        item = self._item(
            acceptance_mode='deployed', status='awaiting_merge', prs='open',
            labels=['service-affecting'],
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('open_linked_pr', str(ctx.exception))

    def test_fixture_plat4732_merge_is_not_enforce_proof(self):
        # PLAT-4732 → PLAT-5228: merge flipped shadow mode only; enforce +
        # hold-not-bounce proof never ran. Merge event must land non-terminal
        # and terminal close must name missing deploy evidence.
        item = self._item(
            acceptance_mode='live', status='awaiting_merge', prs='merged',
            labels=['service-affecting'],
        )
        with mock.patch.object(
            WorkflowService, 'execute_transition', return_value=item,
        ):
            target = WorkflowService.apply_merge_event_transition(item)
        self.assertEqual(target, 'awaiting_deploy')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        msg = str(ctx.exception)
        self.assertIn('missing_correlated_deploy_event', msg)

    def test_fixture_dojo15_live_needs_qa_probe(self):
        # DOJO-15 → DOJO-27: TLS repaired but JWKS still served SPA HTML —
        # deploy event alone is insufficient for live acceptance.
        item = self._item(acceptance_mode='live', prs='merged')
        self._deploy_event(item, live_probe=False)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    def test_fixture_scli304_formatter_only_is_not_live_proof(self):
        # SCLI-304 → SCLI-369: claimed visible interrupt UX; coverage was
        # formatter/registry-only. Free-form "verified live" comments are not
        # authority; live needs QA-authored typed probe or verified_by_qa.
        item = self._item(acceptance_mode='live', prs='merged')
        self._deploy_event(item, live_probe=False)
        self._comment(
            item,
            'interrupt UX verified live in tmux — formatter + registry green',
            email='zen@shizuha.com',
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertIn('missing_live_probe_or_qa', str(ctx.exception))

    # ── kill-switch ──────────────────────────────────────────────────────────

    @override_settings(PULSE_DEPLOY_VERIFY_GATE_ENABLED=False)
    def test_kill_switch_disables_gate(self):
        item = self._item(acceptance_mode='live', labels=['service-affecting'])
        WorkflowService._validate_deploy_verify_gate(item, 'done')

    # ── integration through validate_transition ──────────────────────────────

    def test_validate_transition_enforces_gate(self):
        item = self._item(
            acceptance_mode='deployed', status='in_review', prs='merged',
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.validate_transition(item, 'done')

    def test_receipt_forged_team_same_email_refused_for_live_and_waiver(self):
        # Rei P1: same-email causal actor + forged issuer_team must not mint a
        # gate-qualifying receipt for either live-probe or waiver templates.
        from tasks.services.mechanical_comments import (
            MechanicalCommentError,
            MechanicalCommentFactory,
        )

        item = self._item(acceptance_mode='live', prs='merged')
        actor = self._actor('not-on-roster@evil.example')

        with self.assertRaises(MechanicalCommentError) as live_ctx:
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.live_probe_passed',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'passed',
                    'issuer_email': 'not-on-roster@evil.example',
                    'issuer_team': 'qa',
                    'issued_at': '2026-08-01T00:00:00Z',
                    'deployment_event_id': '00000000-0000-0000-0000-000000000001',
                    'deployment_repo': REPO,
                    'deployment_commit_sha': MERGED_SHA,
                    'deployment_image': 'registry.example/x:abc',
                    'deployment_image_digest': '',
                    'deployment_image_tag': '',
                    'deployment_replicas_ready': True,
                    'deployment_converged_at': '2026-08-01T00:00:00Z',
                    'evidence': 'forged-team-live',
                },
                origin_event_id=f'forged-team-live-only-{item.item_key}',
                causal_actor=actor,
            )
        self.assertIn('not a member', str(live_ctx.exception).lower())

        with self.assertRaises(MechanicalCommentError) as waiver_ctx:
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.deploy_verify_waiver',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'issued',
                    'issuer_email': 'not-on-roster@evil.example',
                    'issuer_team': 'devops',
                    'issued_at': '2026-08-01T00:00:00Z',
                    'reason': 'forged-team-waiver',
                },
                origin_event_id=f'forged-team-waiver-only-{item.item_key}',
                causal_actor=actor,
            )
        self.assertIn('not a member', str(waiver_ctx.exception).lower())

        # Gate must not accept a hand-built comment that claims qa without the
        # factory membership bind (defense in depth still requires causal match,
        # but factory is the mint authority).
        self.assertFalse(WorkflowService._item_has_typed_live_probe(item))
        self.assertFalse(WorkflowService._has_role_gated_deploy_waiver(item))

    def test_merged_awaiting_retry_limit_1_still_attempts(self):
        # Rei P2: sync_linked_pr_statuses(limit=1) is a supported bounded
        # invocation; the dedicated merge-retry lane must still attempt the
        # stored-merged awaiting_merge row (it makes no provider API call).
        import os
        from tasks.tasks import sync_linked_pr_statuses

        stranded = self._item(
            acceptance_mode='deployed',
            status='awaiting_merge',
            prs='merged',
        )

        call_keys = []

        def _exec_side_effect(*, item, new_status, user=None, **kwargs):
            call_keys.append(getattr(item, 'item_key', item.pk))
            Item.objects.filter(pk=item.pk).update(status=new_status)
            item.status = new_status
            return item

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=False), \
             mock.patch.object(
                WorkflowService, '_verify_live_pr_merge_state',
                return_value=('proceed', 'test-stub'),
             ), \
             mock.patch(
                'tasks.services.workflow_service.WorkflowService.execute_transition',
                side_effect=_exec_side_effect,
             ), \
             mock.patch('tasks.tasks.send_alert_notification.delay'), \
             mock.patch(
                'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                return_value=(mock.Mock(id=1), True),
             ):
            sync_linked_pr_statuses.run(limit=1)

        stranded.refresh_from_db()
        self.assertEqual(stranded.status, 'awaiting_deploy')
        self.assertIn(stranded.item_key, call_keys)

    def test_merged_awaiting_retry_not_starved_by_open_api_budget(self):
        # Rei P1: more than one API-budget of preceding open rows must not
        # prevent a stored-merged awaiting_merge target from being attempted.
        import os
        from tasks.tasks import sync_linked_pr_statuses

        stranded = self._item(
            acceptance_mode='deployed',
            status='awaiting_merge',
            prs='merged',
        )
        # Bump updated_at ordering: create many fresher open-PR items that would
        # consume a tiny open-lane budget if the merge retry shared it.
        open_items = []
        for i in range(8):
            open_items.append(self._item(
                acceptance_mode='code',
                status='in_review',
                prs='open',
                item_key=f'PLAT-OPEN-{i}',
            ))

        call_keys = []

        def _exec_side_effect(*, item, new_status, user=None, **kwargs):
            call_keys.append(getattr(item, 'item_key', item.pk))
            Item.objects.filter(pk=item.pk).update(status=new_status)
            item.status = new_status
            return item

        # Provider API for open PRs: always return open so they consume budget
        # if the open lane runs. Dedicated merge lane must not need this.
        class _Resp:
            status_code = 200
            ok = True

            def json(self):
                return {
                    'state': 'open',
                    'merged': False,
                    'html_url': 'https://github.com/shizuha-labs/x/pull/99',
                    'head': {'sha': 'abc1234'},
                }

        def _api_request(url, **kwargs):
            return ('github', 'shizuha-labs', 'x', 99, _Resp())

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=False), \
             mock.patch.object(
                WorkflowService, '_verify_live_pr_merge_state',
                return_value=('proceed', 'test-stub'),
             ), \
             mock.patch(
                'tasks.services.workflow_service.WorkflowService.execute_transition',
                side_effect=_exec_side_effect,
             ), \
             mock.patch('tasks.tasks._linked_pr_api_request', side_effect=_api_request), \
             mock.patch('tasks.tasks.send_alert_notification.delay'), \
             mock.patch(
                'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                return_value=(mock.Mock(id=1), True),
             ):
            # limit=2 → open budget tiny; without dedicated lane the stranded
            # merged row would never be reached behind 8 open candidates.
            sync_linked_pr_statuses.run(limit=2)

        stranded.refresh_from_db()
        self.assertEqual(stranded.status, 'awaiting_deploy')
        self.assertIn(stranded.item_key, call_keys)

    def test_merge_event_failure_is_per_task(self):
        # Rei P2: success on B must not erase pending failure for A.
        from tasks.liveness import (
            clear_merge_event_retry_failure,
            record_merge_event_retry_failure,
            read_merge_event_retry_failure,
        )

        record_merge_event_retry_failure(
            item_key='PLAT-A', error='fail-a', client=self._redis,
        )
        record_merge_event_retry_failure(
            item_key='PLAT-B', error='fail-b', client=self._redis,
        )
        summary = read_merge_event_retry_failure(client=self._redis)
        self.assertIsNotNone(summary)
        self.assertEqual(summary['pending_count'], 2)
        self.assertEqual(set(summary['pending_item_keys']), {'PLAT-A', 'PLAT-B'})

        clear_merge_event_retry_failure(item_key='PLAT-B', client=self._redis)
        summary = read_merge_event_retry_failure(client=self._redis)
        self.assertEqual(summary['pending_count'], 1)
        self.assertEqual(summary['pending_item_keys'], ['PLAT-A'])
        self.assertEqual(summary['item_key'], 'PLAT-A')

        clear_merge_event_retry_failure(item_key='PLAT-A', client=self._redis)
        self.assertIsNone(read_merge_event_retry_failure(client=self._redis))

    def test_issue_live_probe_receipt_api_requires_auth_and_qa(self):
        # Rei P2: exercise the authenticated DRF endpoint, not only the service.
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from tasks.models import ProjectMembership

        User = get_user_model()
        item = self._item(acceptance_mode='live', prs='merged')
        # Correlated deploy must exist before mint (rui P1 deployment bind).
        deploy = self._deploy_event(item, live_probe=False)
        client = APIClient()

        # Unauthenticated → 401/403.
        resp = client.post(
            f'/api/items/{item.pk}/issue-live-probe-receipt/',
            {'evidence': 'GET /health 200'},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))

        qa = User.objects.create_user(
            username='qa-api-user', email='qa-api@shizuha.com', password='x',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=qa.id,
            user_email=qa.email,
            role='member',
        )
        client.force_authenticate(user=qa)
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_object_permission',
            return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_permission',
            return_value=True,
        ):
            ok = client.post(
                f'/api/items/{item.pk}/issue-live-probe-receipt/',
                {'evidence': 'GET /health 200'},
                format='json',
            )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.data['caused_by_email_snapshot'], 'qa-api@shizuha.com')
        self.assertEqual(ok.data['origin_payload']['issuer_email'], 'qa-api@shizuha.com')
        self.assertEqual(
            str(ok.data['origin_payload']['deployment_event_id']), str(deploy.pk),
        )

        # Non-QA team member refused at the service gate (403).
        outsider = User.objects.create_user(
            username='outsider', email='outsider@evil.example', password='x',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=outsider.id,
            user_email=outsider.email,
            role='member',
        )
        client.force_authenticate(user=outsider)
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=False,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_object_permission',
            return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_permission',
            return_value=True,
        ):
            denied = client.post(
                f'/api/items/{item.pk}/issue-live-probe-receipt/',
                {'evidence': 'forged'},
                format='json',
            )
        self.assertEqual(denied.status_code, 403)

    def test_issue_live_probe_receipt_api_accepts_inline_package_envelope(self):
        # PLAT-9244 regression: the documented QA envelope carries `evidence`
        # as a dict (inline package). The endpoint previously blind-stripped
        # it (AttributeError → deterministic 500); it must mint 201 like the
        # sibling waiver endpoint does with the same envelope.
        import json as _json

        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from tasks.models import ProjectMembership

        User = get_user_model()
        item = self._item(acceptance_mode='live', prs='merged')
        deploy = self._deploy_event(item, live_probe=False)
        client = APIClient()
        qa = User.objects.create_user(
            username='qa-pkg-user', email='qa-pkg@shizuha.com', password='x',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=qa.id,
            user_email=qa.email,
            role='member',
        )
        client.force_authenticate(user=qa)
        envelope = {
            'kind': 'inline_package',
            'actor': 'qa-pkg@shizuha.com',
            'originating_key': item.item_key,
            'reason': 'live focused-QA probe of the deployed surface',
            'target_head': MERGED_SHA,
            'environment': 'production',
            'probed_urls': ['https://pulse.example.com/api/health/'],
            'verdict_refs': ['focused-qa:PASS'],
            'scope_conditions': ['read-only probes'],
        }
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_object_permission',
            return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_permission',
            return_value=True,
        ), mock.patch.object(
            WorkflowService, '_find_correlated_deploy_event', return_value=deploy,
        ):
            ok = client.post(
                f'/api/items/{item.pk}/issue-live-probe-receipt/',
                {'evidence': envelope},
                format='json',
            )
        self.assertEqual(ok.status_code, 201, ok.content)
        # Template v1 pins evidence: str — the typed package is preserved as
        # canonical JSON inside the string, round-trippable for auditors.
        stored = ok.data['origin_payload']['evidence']
        self.assertIsInstance(stored, str)
        self.assertEqual(_json.loads(stored)['kind'], 'inline_package')
        self.assertEqual(_json.loads(stored)['target_head'], MERGED_SHA)

    def test_issue_live_probe_receipt_api_malformed_envelope_is_400(self):
        # PLAT-9244: a well-typed but incomplete envelope is a client error —
        # 400 naming the missing fields, never a 500 and never a bare 403.
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from tasks.models import ProjectMembership

        User = get_user_model()
        item = self._item(acceptance_mode='live', prs='merged')
        deploy = self._deploy_event(item, live_probe=False)
        client = APIClient()
        qa = User.objects.create_user(
            username='qa-bad-user', email='qa-bad@shizuha.com', password='x',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=qa.id,
            user_email=qa.email,
            role='member',
        )
        client.force_authenticate(user=qa)
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_object_permission',
            return_value=True,
        ), mock.patch(
            'tasks.permissions.ItemProjectPermission.has_permission',
            return_value=True,
        ), mock.patch.object(
            WorkflowService, '_find_correlated_deploy_event', return_value=deploy,
        ):
            bad = client.post(
                f'/api/items/{item.pk}/issue-live-probe-receipt/',
                {'evidence': {'kind': 'inline_package', 'actor': 'qa-bad@shizuha.com'}},
                format='json',
            )
            # Non-str/dict evidence is refused by the view type gate (400).
            wrong = client.post(
                f'/api/items/{item.pk}/issue-live-probe-receipt/',
                {'evidence': ['not', 'a', 'package']},
                format='json',
            )
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(bad.data['error'], 'invalid_evidence')
        self.assertIn('target_head', bad.data['detail'])
        self.assertEqual(wrong.status_code, 400, wrong.content)
        self.assertEqual(wrong.data['error'], 'invalid_evidence')

    def test_corrective_pr_reentry_runs_review_merge_deploy_sequence(self):
        corrective, review_passed, merge, _close = (
            self._seed_corrective_review_workflow()
        )
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[self._merged_pr(), self._corrective_pr()],
            created_by_email='reporter@shizuha.com',
        )
        self._set_test_owner(item)
        self.assertTrue(
            WorkflowService._transition_is_visible_for_item(corrective, item),
        )

        def _resolve(team_name, *args, **kwargs):
            if team_name == 'review':
                return 22, 'reviewer@shizuha.com'
            if team_name == 'merge':
                return 33, 'merger@shizuha.com'
            return None, None

        with mock.patch.object(
            WorkflowService,
            '_refresh_review_reentry_pr_metadata',
            side_effect=lambda current: self._confirmed_prs(current),
        ), mock.patch.object(
            WorkflowService,
            '_origin_pr_author_login',
            return_value=('author', None),
        ), mock.patch.object(
            WorkflowService,
            '_forgejo_current_head_reviewers',
            return_value=({'reviewer'}, None),
        ), mock.patch.object(
            WorkflowService, '_resolve_team_assignee', side_effect=_resolve,
        ):
            WorkflowService.execute_transition(
                item,
                'in_review',
                user=self._actor('author@shizuha.com'),
                transition_id=corrective.id,
            )
            item.refresh_from_db()
            self.assertEqual(item.status, 'in_review')
            self.assertEqual(item.assignment_group, 'review')
            self.assertEqual(item.assignee_email, 'reviewer@shizuha.com')

            WorkflowService.execute_transition(
                item,
                'awaiting_merge',
                user=self._actor('reviewer@shizuha.com'),
                transition_id=review_passed.id,
            )
            item.refresh_from_db()
            self.assertEqual(item.status, 'awaiting_merge')
            self.assertEqual(item.assignment_group, 'merge')
            self.assertEqual(item.assignee_email, 'merger@shizuha.com')

            metadata = dict(item.metadata or {})
            prs = [dict(pr) for pr in metadata['pull_requests']]
            prs[1]['status'] = 'merged'
            prs[1]['merged_at'] = '2026-08-08T12:00:00Z'
            metadata['pull_requests'] = prs
            item.metadata = metadata
            item.save(update_fields=['metadata', 'updated_at'])

            WorkflowService.execute_transition(
                item,
                'awaiting_deploy',
                user=self._actor('merger@shizuha.com'),
                transition_id=merge.id,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_deploy')
        self.assertEqual(
            item.metadata['implementation_return_context']['implementer']['email'],
            'author@shizuha.com',
        )

    def test_corrective_pr_reentry_rejects_noncurrent_or_nonorigin_evidence(self):
        corrective, _review_passed, _merge, _close = (
            self._seed_corrective_review_workflow()
        )
        mirror = self._corrective_pr(
            url='https://github.com/shizuha-labs/x/pull/2',
        )
        cases = (
            ('no corrective PR', [self._merged_pr()], True, False),
            ('unconfirmed provider head', [
                self._merged_pr(), self._corrective_pr(),
            ], False, True),
            ('retired mirror', [self._merged_pr(), mirror], True, False),
            ('same merged head', [
                self._merged_pr(), self._corrective_pr(sha=MERGED_SHA),
            ], True, False),
            ('corrective already merged', [
                self._merged_pr(), self._corrective_pr(status='merged'),
            ], True, False),
            ('corrective closed', [
                self._merged_pr(), self._corrective_pr(status='closed'),
            ], True, False),
            ('no-pr marker is not a corrective PR', [
                self._merged_pr(), {'url': 'no-pr'},
            ], True, False),
            ('no prior merged head', [self._corrective_pr()], True, False),
        )

        for label, prs, provider_confirmed, visible in cases:
            with self.subTest(label=label):
                item = self._item(
                    acceptance_mode='deployed',
                    status='awaiting_deploy',
                    prs=prs,
                    created_by_email='reporter@shizuha.com',
                )
                self._set_test_owner(item)
                self.assertEqual(
                    WorkflowService._transition_is_visible_for_item(
                        corrective, item,
                    ),
                    visible,
                )
                with mock.patch.object(
                    WorkflowService,
                    '_refresh_review_reentry_pr_metadata',
                    side_effect=lambda current, confirmed=provider_confirmed: (
                        self._confirmed_prs(current, confirmed=confirmed)
                    ),
                ):
                    with self.assertRaises(WorkflowValidationError) as ctx:
                        WorkflowService.execute_transition(
                            item,
                            'in_review',
                            user=self._actor('author@shizuha.com'),
                            transition_id=corrective.id,
                        )
                self.assertIn('Corrective-PR review re-entry', str(ctx.exception))
                item.refresh_from_db()
                self.assertEqual(item.status, 'awaiting_deploy')

    def test_no_pr_marker_does_not_obscure_valid_corrective_origin_pr(self):
        corrective, _review_passed, _merge, _close = (
            self._seed_corrective_review_workflow()
        )
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[
                self._merged_pr(),
                {'url': 'no-pr'},
                self._corrective_pr(),
            ],
            created_by_email='reporter@shizuha.com',
        )
        self._set_test_owner(item)

        self.assertTrue(
            WorkflowService._transition_is_visible_for_item(corrective, item),
        )
        with mock.patch.object(
            WorkflowService,
            '_refresh_review_reentry_pr_metadata',
            side_effect=lambda current: self._confirmed_prs(current),
        ):
            allowed, detail = (
                WorkflowService._awaiting_deploy_corrective_pr_is_current(item)
            )
        self.assertTrue(allowed, detail)

    def test_corrective_pr_reentry_records_provider_current_exact_head(self):
        current_head = 'a11cefacecafe1234567890abcdef1234567890ab'
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[
                self._merged_pr(),
                self._corrective_pr(sha=CORRECTIVE_SHA),
            ],
        )

        def _provider_request(url, **_kwargs):
            is_corrective = url.rstrip('/').endswith('/pulls/2')
            payload = {
                'state': 'open' if is_corrective else 'closed',
                'merged': not is_corrective,
                'head': {
                    'sha': current_head if is_corrective else MERGED_SHA,
                },
            }
            response = mock.Mock(ok=True, status_code=200)
            response.json.return_value = payload
            return (
                'forgejo', 'shizuha-labs', 'x',
                2 if is_corrective else 1,
                response,
            )

        with mock.patch.dict(
            'os.environ',
            {
                'FORGEJO_TOKEN': 'test-token',
                'ORIGIN_URL': 'https://origin.shizuha.com',
                'PULSE_SUBSTRATE_CAP_MODE': 'off',
            },
            clear=True,
        ), mock.patch(
            'tasks.tasks._linked_pr_api_request',
            side_effect=_provider_request,
        ):
            allowed, detail = (
                WorkflowService._awaiting_deploy_corrective_pr_is_current(item)
            )

        self.assertTrue(allowed, detail)
        item.refresh_from_db()
        corrective = item.metadata['pull_requests'][1]
        self.assertEqual(corrective['status'], 'open')
        self.assertEqual(corrective['head_sha'], current_head)
        self.assertTrue(corrective['last_synced_at'])

    def test_corrective_pr_reentry_action_is_hidden_until_candidate_exists(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        self._seed_corrective_review_workflow()
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[self._merged_pr()],
        )
        user = get_user_model().objects.create_user(
            username='pls902-admin',
            email='pls902-admin@shizuha.com',
            password='x',
            is_staff=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        url = f'/api/items/{item.item_key}/available-transitions/'

        without_candidate = client.get(url)
        self.assertEqual(without_candidate.status_code, 200, without_candidate.data)
        self.assertNotIn(
            'Submit Corrective PR for Code Review',
            [row['name'] for row in without_candidate.data['transitions']],
        )

        metadata = dict(item.metadata or {})
        metadata['pull_requests'] = [
            self._merged_pr(), self._corrective_pr(),
        ]
        item.metadata = metadata
        item.save(update_fields=['metadata', 'updated_at'])

        with_candidate = client.get(url)
        self.assertEqual(with_candidate.status_code, 200, with_candidate.data)
        self.assertIn(
            'Submit Corrective PR for Code Review',
            [row['name'] for row in with_candidate.data['transitions']],
        )

    def test_corrective_pr_author_cannot_take_review_assignment(self):
        corrective, _review_passed, _merge, _close = (
            self._seed_corrective_review_workflow()
        )
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[self._merged_pr(), self._corrective_pr()],
            created_by_email='reporter@shizuha.com',
        )
        self._set_test_owner(item)

        def _author_only_resolver(team_name, *args, **kwargs):
            self.assertEqual(team_name, 'review')
            routed_item = kwargs['item']
            reasons = WorkflowService._review_role_hard_policy_reasons(
                routed_item,
                'review',
                'author@shizuha.com',
                user_id=11,
                exclude_email=kwargs.get('exclude_email'),
            )
            self.assertIn('review_sod_author_identity', reasons)
            with mock.patch.object(
                WorkflowService, '_routability_enforced', return_value=False,
            ):
                self.assertFalse(
                    WorkflowService._seat_assignment_allowed(
                        WorkflowService._RoutabilityVerdict(
                            ok=False,
                            reasons=reasons,
                        )
                    )
                )
            return None, None

        with mock.patch.object(
            WorkflowService,
            '_refresh_review_reentry_pr_metadata',
            side_effect=lambda current: self._confirmed_prs(current),
        ), mock.patch.object(
            WorkflowService,
            '_origin_pr_author_login',
            return_value=('author', None),
        ) as author_lookup, mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_seat_satisfies_required_capability',
            return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=_author_only_resolver,
        ):
            WorkflowService.execute_transition(
                item,
                'in_review',
                user=self._actor('author@shizuha.com'),
                transition_id=corrective.id,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertFalse(item.assignee_id)
        self.assertFalse(item.assignee_email)
        self.assertTrue(
            any(
                call.kwargs.get('require_exact_head') is True
                for call in author_lookup.call_args_list
            )
        )

    def test_corrective_pr_keeps_deploy_close_fail_closed_while_open(self):
        _corrective, _review_passed, _merge, close = (
            self._seed_corrective_review_workflow()
        )
        item = self._item(
            acceptance_mode='deployed',
            status='awaiting_deploy',
            prs=[self._merged_pr(), self._corrective_pr()],
        )

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item,
                'done',
                user=self._actor('devops@shizuha.com'),
                transition_id=close.id,
            )
        self.assertIn('open', str(ctx.exception).lower())
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_deploy')

    # ── package-publish without deploy surface is not deploy-governed (PLS-1047) ──

    # A package-publish task (e.g. ORIG-232 @shizuha/ui) delivers a published
    # package version (npm/Verdaccio), not a k8s DeploymentEvent — it has no
    # repo/PR/merge/deploy of its own that produces a correlated deployment.
    # The deploy-verify gate must never demand a merged-head SHA from it, even
    # when risk labels (user-facing / infra-control-plane) would otherwise
    # default acceptance_mode to live. The exemption is an explicit
    # ``package-publish`` marker so it can never over-exempt deploy-governed
    # work.

    def _package_publish_item(self, **kw):
        """Package-publish artifact explicitly marked ``package-publish``."""
        defaults = dict(
            labels={
                'risk_label': 'payments-books',
                'risk_labels': ['user-facing', 'payments-books'],
                'environment': 'live',
                'surface': 'package-publish',
            },
            tags='user-facing,package-publish,payments-books',
            workflow_name='autonomous-bug',
            item_type='task',
            source='origin',
            prs='none',
            metadata={
                'package_name': '@shizuha/ui',
                'package_version': '1.2.6',
            },
        )
        defaults.update(kw)
        return self._item(**defaults)

    def test_package_publish_resolves_code_even_with_risk_labels(self):
        # Risk labels describe the surface the package feeds, not that the task
        # is deployable — an explicitly-marked package-publish task must resolve
        # to code.
        item = self._package_publish_item()
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'code',
        )

    def test_package_publish_create_persists_code_default(self):
        # Rei P2-5: the typed default is persisted at insert — a package-publish
        # task must not be persisted as live just because it carries risk labels.
        item = self._package_publish_item()
        item.refresh_from_db()
        self.assertEqual(item.acceptance_mode, 'code')

    def test_package_publish_closes_without_deploy_evidence(self):
        # The gate must not demand a merged-head SHA from a package-publish task.
        item = self._package_publish_item()
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['last_result'], 'pass_code')
        self.assertEqual(liveness['missing_classes'], [])

    def test_package_publish_with_persisted_live_field_still_closes(self):
        # Durable safety net: even a row whose acceptance_mode field was
        # persisted as live before the create-time default was corrected must
        # close (the gate exempts package-publish regardless of field).
        item = self._package_publish_item(acceptance_mode='live')
        WorkflowService._validate_deploy_verify_gate(item, 'done')
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        # Governed mode with complete (exempted) evidence records pass_event;
        # the key assertion is that the gate does NOT refuse close.
        self.assertEqual(liveness['last_result'], 'pass_event')
        self.assertEqual(liveness['missing_classes'], [])

    def test_package_publish_marker_does_not_exempt_deploy_governed(self):
        # The explicit marker must NOT over-exempt deploy-governed work: a task
        # carrying the marker but with a projectable deployment event stays
        # governed and still demands deploy evidence.
        item = self._package_publish_item(
            acceptance_mode='deployed',
            metadata={
                'package_name': '@shizuha/ui',
                'focused_qa_deployment_event_id': 'DEP-1',
            },
        )
        missing = WorkflowService._collect_deploy_verify_missing(
            item, 'deployed',
        )
        self.assertNotIn('missing_correlated_deploy_event', missing)
        self.assertTrue(missing)

    def test_package_publish_marker_required_no_over_exemption(self):
        # A task WITHOUT the explicit marker is NOT exempted — user-facing work
        # without the marker still defaults to live and demands deploy evidence.
        item = self._item(
            labels={'risk_label': 'payments-books', 'environment': 'live'},
            tags='user-facing,payments-books',
            prs=[self._merged_pr()],
        )
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'live',
        )
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn('missing_correlated_deploy_event', missing)


class ReceiptSelfAuditingTests(DeployVerifyGateV4Tests):
    """PLAT-8177 — receipts must be self-auditing.

    A receipt that cannot be interpreted without off-object context (DM
    history, unlinked prose) is a provenance gap. Every NEW waiver mint must
    carry EITHER the full evidence package inline OR an immutable,
    content-addressed trail-pointer; mints without one are rejected at mint
    time. v1 receipts are grandfathered with a documented composite-record
    location (the item trail resolved by structured_payload.item_key).
    """

    def _mint(self, item, *, email='devops@shizuha.com', evidence='OMIT'):
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            kwargs = {
                'actor': self._actor(email),
                'reason': 'config-only change verified live',
                'event_id': f'test-8177-{item.item_key}-{email}',
            }
            if evidence != 'OMIT':
                kwargs['evidence'] = evidence
            return WorkflowService.issue_deploy_verify_waiver(item, **kwargs)

    def test_mint_without_evidence_is_rejected(self):
        """Negative test: no package, no pointer -> rejected at mint time."""
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._mint(item, evidence=None)
        self.assertIn('PLAT-8177', str(ctx.exception))

    def test_mint_with_unknown_evidence_kind_is_rejected(self):
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError):
            self._mint(item, evidence={'kind': 'prose', 'note': 'see DMs'})

    def test_inline_package_missing_keys_is_rejected(self):
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._mint(item, evidence={
                'kind': 'inline_package',
                'actor': 'devops@shizuha.com',
                # missing originating_key / reason / target_head / lists
            })
        self.assertIn('missing or malformed', str(ctx.exception))

    def test_trail_pointer_without_content_address_is_rejected(self):
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError) as ctx:
            self._mint(item, evidence={
                'kind': 'trail_pointer',
                'record_kind': 'comment',
                'record_id': '664569',
                # content_address missing — an unaddressed pointer is not
                # immutable and does not satisfy PLAT-8177
            })
        self.assertIn('content_address', str(ctx.exception))

    def test_trail_pointer_with_bad_digest_is_rejected(self):
        item = self._item(acceptance_mode='deployed')
        with self.assertRaises(WorkflowValidationError):
            self._mint(item, evidence={
                'kind': 'trail_pointer',
                'record_kind': 'comment',
                'record_id': '664569',
                'content_address': 'sha256:not-a-digest',
            })

    def test_inline_package_mint_carries_the_full_package(self):
        item = self._item(acceptance_mode='deployed')
        receipt = self._mint(item, evidence={
            'kind': 'inline_package',
            'actor': 'devops@shizuha.com',
            'originating_key': item.item_key,
            'reason': 'config-only change verified live',
            'target_head': MERGED_SHA,
            'verdict_refs': ['focused-qa:PASS', 'CTX-890 close-out'],
            'scope_conditions': ['config-only', 'no schema change'],
        })
        payload = receipt.origin_payload
        self.assertEqual(receipt.template_version, 2)
        self.assertEqual(payload['evidence_kind'], 'inline_package')
        self.assertEqual(payload['evidence']['target_head'], MERGED_SHA)
        self.assertEqual(payload['evidence']['verdict_refs'],
                         ['focused-qa:PASS', 'CTX-890 close-out'])
        self.assertEqual(payload['evidence']['scope_conditions'],
                         ['config-only', 'no schema change'])

    def test_trail_pointer_mint_is_content_addressed(self):
        item = self._item(acceptance_mode='deployed')
        digest = 'sha256:' + 'ab' * 32
        receipt = self._mint(item, evidence={
            'kind': 'trail_pointer',
            'record_kind': 'comment',
            'record_id': '664569',
            'content_address': digest,
        })
        payload = receipt.origin_payload
        self.assertEqual(receipt.template_version, 2)
        self.assertEqual(payload['evidence_kind'], 'trail_pointer')
        self.assertEqual(payload['evidence']['content_address'], digest)
        self.assertEqual(payload['evidence']['record_id'], '664569')

    def test_v1_receipts_are_grandfathered(self):
        """v1 template stays mintable; composite-record location is the trail."""
        from tasks.services.mechanical_comments import MechanicalCommentFactory
        item = self._item(acceptance_mode='deployed')
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            receipt = MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='receipt.deploy_verify_waiver',
                template_version=1,
                structured_payload={
                    'item_key': item.item_key,
                    'result': 'issued',
                    'issuer_email': 'devops@shizuha.com',
                    'issuer_team': 'devops',
                    'issued_at': '2026-09-09T00:00:00+00:00',
                    'reason': 'pre-PLAT-8177 mint',
                },
                origin_event_id='test-8177-grandfathered-v1',
                causal_actor=self._actor('devops@shizuha.com'),
            )
        self.assertEqual(receipt.template_version, 1)
        # The documented composite-record location: the item trail resolved
        # by the receipt's own item_key.
        self.assertEqual(receipt.origin_payload['item_key'], item.item_key)


class MergeEventAmbiguousTransitionTests(TestCase):
    """PLS-909: merge-event retries must resolve the PR-merge transition by an
    explicit semantic selector (never the no-PR/docs close path), and stale
    per-item retry failures must be cleared when the item advances out of band.
    """

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 1
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        self.record_heartbeat = patcher.start()
        self.addCleanup(patcher.stop)

        # The autonomous-bug workflow row is seeded by migration 0141, but the
        # awaiting_merge → done edges are NOT (migration 0067 runs before 0141
        # so it found no workflow and created nothing). Build the ambiguous
        # same-target fixture explicitly: the no-PR/docs close path AND the
        # PR-merge path both go awaiting_merge → done.
        from tasks.models import Workflow, WorkflowTransition
        self.wf = Workflow.objects.filter(slug='autonomous-bug').first()
        if self.wf is None:
            self.wf = Workflow.objects.create(
                name='Autonomous Bug', slug='autonomous-bug',
                description='PLS-909 fixture workflow.',
            )
        WorkflowTransition.objects.get_or_create(
            workflow=self.wf,
            name='Approve & Close (no PR / docs)',
            from_status='awaiting_merge',
            to_status='done',
            defaults={'position': 34, 'description': 'no-PR/docs close path'},
        )
        WorkflowTransition.objects.get_or_create(
            workflow=self.wf,
            name='Merge & Close',
            from_status='awaiting_merge',
            to_status='done',
            defaults={'position': 35, 'description': 'PR-merge path'},
        )

    def _item(self, **kw):
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        kw.setdefault('status', 'awaiting_merge')
        kw.setdefault('workflow_name', 'autonomous-bug')
        defaults = dict(
            project=self.project,
            title='[BUG] merge-event ambiguity',
            description='PLS-909 regression fixture.',
            labels=[],
            tags='',
            item_type='task',
        )
        defaults.update(kw)
        return Item.objects.create(**defaults)

    def test_merge_event_transition_selects_pr_merge_not_no_pr_close(self):
        # The autonomous-bug workflow exposes BOTH "Approve & Close (no PR /
        # docs)" and the PR-merge path from awaiting_merge → done (migration
        # 0067). The selector must pick the PR-merge transition and never the
        # no-PR/docs close.
        item = self._item()
        from tasks.models import WorkflowTransition
        from tasks.services.workflow_service import WorkflowService

        transitions = WorkflowTransition.objects.filter(
            workflow=self.wf,
            from_status='awaiting_merge',
            to_status='done',
        )
        self.assertGreaterEqual(transitions.count(), 2)

        selected = WorkflowService._merge_event_transition(item, 'done')
        self.assertIsNotNone(selected)
        self.assertNotEqual(
            selected.name, 'Approve & Close (no PR / docs)',
            'the no-PR/docs close transition must never be fired for a merge event',
        )

    def test_merge_event_transition_fails_loud_when_ambiguous_after_exclusion(self):
        # If (hypothetically) more than one non-no-PR transition exists, the
        # selector must fail loud with actionable evidence rather than guess.
        item = self._item()
        from tasks.models import WorkflowTransition
        from tasks.services.workflow_service import WorkflowService

        # Create a second non-no-PR awaiting_merge → done transition.
        WorkflowTransition.objects.create(
            workflow=self.wf,
            name='Merge & Close (duplicate)',
            from_status='awaiting_merge',
            to_status='done',
            position=99,
        )
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._merge_event_transition(item, 'done')
        self.assertIn('PLS-909', str(ctx.exception))

    def test_attempt_merge_event_retry_clears_stale_failure_for_advanced_item(self):
        # Production-order: retry fails/pends → task advances out of band →
        # next reconciliation clears ONLY that task's failure, preserving a
        # second task's pending failure.
        from tasks.tasks import _attempt_merge_event_retry

        advanced = self._item(status='done')
        still_pending = self._item(status='awaiting_merge')

        cleared = []
        with mock.patch(
            'tasks.liveness.clear_merge_event_retry_failure',
            side_effect=lambda *, item_key: cleared.append(item_key),
        ):
            result = _attempt_merge_event_retry(advanced)

        self.assertFalse(result)
        self.assertIn(advanced.item_key, cleared)
        self.assertNotIn(still_pending.item_key, cleared)
# ── PLAT-8967: merge-time review gate ────────────────────────────────────────
# Prevention for PLAT-8962 (Retro 09-16): a PR merged with an empty review
# board advanced silently. The merge-event lane now requires ≥1 qualifying
# review entry (reviewer ∉ {author, merger}, exact current head) or a typed
# emergency-merge marker; silent empty-review merges are refused with a
# durable breach record.


@override_settings(PULSE_MERGE_REVIEW_GATE_ENABLED=True)
class PLAT8967MergeReviewGateTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 9700
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        self.record_heartbeat = patcher.start()
        self.addCleanup(patcher.stop)
        self._redis = _DictRedis()
        redis_patcher = mock.patch(
            'tasks.liveness._redis_client', return_value=self._redis,
        )
        redis_patcher.start()
        self.addCleanup(redis_patcher.stop)

    def _item(self, *, prs='merged', status='awaiting_merge'):
        self._next_item_number += 1
        metadata = {}
        if prs == 'merged':
            metadata['pull_requests'] = [{
                'url': f'https://origin.shizuha.com/{REPO}/pulls/1',
                'repo': REPO,
                'number': 1,
                'status': 'merged',
                'merged_at': '2026-09-01T00:00:00Z',
                'head_sha': MERGED_SHA,
            }]
        elif prs == 'none':
            metadata['pull_requests'] = []
        return Item.objects.create(
            project=self.project,
            item_key=f'PLAT-{self._next_item_number}',
            title='[BUG] merge gate datum',
            description='Service-affecting regression.',
            labels=[],
            tags='',
            status=status,
            workflow_name='autonomous-bug',
            item_type='task',
            created_by_id=1,
            created_by_email='author@shizuha.com',
            metadata=metadata,
        )

    def _identity_patch(self, authors=('jun',), reviewers=(),
                        author_errors=(), review_errors=()):
        return ExitStack()  # caller enters patches

    def test_verdict_ok_with_qualifying_reviewer(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'jun'}, [], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=({'revi'}, [], 1),
        ):
            ok, detail = WorkflowService._merge_review_gate_verdict(item)
        self.assertTrue(ok)
        self.assertIn('revi', detail)

    def test_verdict_refuses_author_as_reviewer(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'jun'}, [], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=({'jun'}, [], 1),
        ):
            ok, detail = WorkflowService._merge_review_gate_verdict(item)
        self.assertFalse(ok)
        self.assertIn('empty_review_board', detail)

    def test_verdict_refuses_merger_as_only_reviewer(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'jun'}, [], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=({'revi'}, [], 1),
        ), mock.patch.object(
            WorkflowService, '_agent_github_identity',
            return_value='revi',
        ):
            ok, detail = WorkflowService._merge_review_gate_verdict(
                item, merged_by='revi@shizuha.com',
            )
        self.assertFalse(ok)

    def test_verdict_ok_with_emergency_marker(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            WorkflowService.issue_emergency_merge_marker(
                item,
                actor=type('Actor', (), {'id': 7, 'email': 'lead@shizuha.com'})(),
                reason='break-glass outage recovery; review post-hoc',
                event_id=f'test-emergency-{item.item_key}',
            )
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'jun'}, [], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=(set(), [], 1),
        ):
            ok, detail = WorkflowService._merge_review_gate_verdict(item)
        self.assertTrue(ok)
        self.assertEqual(detail, 'emergency_merge_marker')

    def test_marker_refuses_automation_and_empty_reason(self):
        item = self._item()
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.issue_emergency_merge_marker(
                item,
                actor=type('Actor', (), {'id': 0, 'email': 'system@shizuha.com'})(),
                reason='automated',
                event_id=f'test-emergency-auto-{item.item_key}',
            )
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService.issue_emergency_merge_marker(
                    item,
                    actor=type('Actor', (), {'id': 7, 'email': 'lead@shizuha.com'})(),
                    reason='',
                    event_id=f'test-emergency-noreason-{item.item_key}',
                )

    def test_verdict_no_pr_item_skips(self):
        item = self._item(prs='none')
        ok, detail = WorkflowService._merge_review_gate_verdict(item)
        self.assertTrue(ok)
        self.assertEqual(detail, 'no_pr_item')

    def test_verdict_fails_closed_on_lookup_error(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=(set(), ['forgejo_failed'], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=({'revi'}, [], 1),
        ):
            ok, detail = WorkflowService._merge_review_gate_verdict(item)
        self.assertFalse(ok)
        self.assertEqual(detail, 'merge_review_evidence_unavailable')

    def test_merge_event_refused_writes_breach_record(self):
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_verify_live_pr_merge_state',
            return_value=('proceed', 'live_merged=true'),
        ), mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'jun'}, [], True),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=(set(), [], 1),
        ):
            result = WorkflowService.apply_merge_event_transition(
                item, pr_html_url=f'https://origin.shizuha.com/{REPO}/pulls/1',
                merged_by='jun@shizuha.com',
            )
        self.assertIsNone(result)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertTrue(
            Comment.objects.filter(
                item=item, origin_event='merge_gate.empty_review_refused',
            ).exists()
        )
