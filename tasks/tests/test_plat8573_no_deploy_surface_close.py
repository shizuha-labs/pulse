"""PLAT-8573: the deploy-verify close gate gains a marker-opt-in
no-deploy-surface close class for docs/design deliverables (PLS-1218
extension; PLAT-7421 Defect-C architect ruling, comment 709935).

HLDs and design dossiers have NO deploy surface of their own: a correlated
production DeploymentEvent is structurally unsatisfiable for them, so the
merged-head evidence class (PLS-675/PLAT-1351 v4) strands verified-complete
work (fixture: BRW-49 — security sign-off 692543 posted, close denied with
the misleading ``missing_merged_head_sha``).

Contract locked here (fail-closed everywhere):
- the class is OPT-IN per task via the explicit ``no-deploy-surface``
  label/tag marker — absence of the marker preserves today's behavior
  byte-for-byte (vocabulary isolation: the focused-QA non-deploy token set
  is NOT widened and never arms this class);
- ANY merged PR head keeps the task on the full PLS-675 path (rigor
  preserved for code-PR work — the marker can never over-exempt);
- focused-QA legs stay governed by PLAT-7600 (the marker does not reroute
  them);
- close evidence is a posted review/sign-off receipt whose server-stamped
  ``Comment.author_email`` belongs to the Security or Architecture roster
  (canonical comment-authority proof, PLS-481/PLS-634 helpers) and whose
  content approves; automation identities, non-roster authors, and
  non-approving prose project nothing;
- missing proof denies with the explicit class code
  ``missing_signoff_receipt`` — ``missing_merged_head_sha`` is never
  emitted for this class.
"""
from django.test import TestCase

from tasks.services.workflow_service import WorkflowService


MERGED_SHA = 'deadbeefcafe1234567890abcdef1234567890ab'
REPO = 'shizuha-labs/x'
SECURITY_EMAIL = 'ren@shizuha.com'
ARCHITECTURE_EMAIL = 'aoi@shizuha.com'
ENGINEERING_EMAIL = 'jun@shizuha.com'


class NoDeploySurfaceCloseTests(TestCase):
    """PLAT-8573 — marker-opt-in docs/design close class.

    Deliberately NOT a subclass of the PLS-675/PLAT-1351 or PLS-1218 suites:
    subclassing would re-run those suites a second time. Only the fixture
    factories are replicated here (kept in sync with their files).
    """

    def setUp(self):
        from tasks.models import Project, TeamRoster

        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self._next_item_number = 1
        # The seeded test DB already carries (shizuha, security/architecture)
        # rosters (uniq_team_roster_org_name) — replace them with fixtures
        # whose members this suite controls (delete-then-create, mirroring
        # test_business_roster_canonicalization).
        TeamRoster.objects.filter(
            organization_slug='shizuha',
            team_name__in=['security', 'architecture'],
        ).delete()
        TeamRoster.objects.create(
            organization_slug='shizuha',
            team_name='security',
            tiers=[[SECURITY_EMAIL]],
            junior_emails=[],
            is_active=True,
        )
        TeamRoster.objects.create(
            organization_slug='shizuha',
            team_name='architecture',
            tiers=[[ARCHITECTURE_EMAIL]],
            junior_emails=[],
            is_active=True,
        )

    # ── factories (mirrored from test_pls1218_no_pr_verification_close) ─────

    def _merged_pr(self, *, sha=MERGED_SHA, repo=REPO):
        return {
            'url': f'https://origin.shizuha.com/{repo}/pulls/1',
            'repo': repo,
            'number': 1,
            'status': 'merged',
            'merged_at': '2026-07-03T00:00:00Z',
            'head_sha': sha,
        }

    def _item(
        self,
        *,
        labels=None,
        tags='',
        workflow_name='autonomous-bug',
        acceptance_mode='live',
        prs='none',
        status='in_review',
        metadata=None,
        **kw,
    ):
        from tasks.models import Item

        metadata = dict(metadata or {})
        if prs == 'merged':
            metadata.setdefault('pull_requests', [self._merged_pr()])
        elif prs == 'none':
            metadata.setdefault('pull_requests', [])
        elif isinstance(prs, list):
            metadata.setdefault('pull_requests', prs)
        kw.setdefault('item_key', f'PLAT-{self._next_item_number}')
        self._next_item_number += 1
        create_kw = dict(
            project=self.project,
            title='[HLD] platform topology design dossier',
            description='Docs/design deliverable with no deploy surface.',
            labels=labels if labels is not None else [],
            tags=tags,
            status=status,
            workflow_name=workflow_name,
            item_type='task',
            created_by_id=1,
            created_by_email=ENGINEERING_EMAIL,
            metadata=metadata,
        )
        create_kw['acceptance_mode'] = acceptance_mode
        create_kw.update(kw)
        return Item.objects.create(**create_kw)

    def _signoff(self, item, *, author_email=SECURITY_EMAIL,
                 content='security sign-off: approved'):
        from tasks.models import Comment

        return Comment.objects.create(
            item=item,
            author_id=100,
            author_email=author_email,
            content=content,
        )

    # ── (a) the class closes: no merged head + marker + sign-off proof ──────

    def test_no_merged_head_marker_security_signoff_closes(self):
        item = self._item(tags='no-deploy-surface')
        self._signoff(item)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_no_merged_head_marker_architecture_signoff_closes(self):
        """The BRW-49 sign-off class accepts Security OR Architecture."""
        item = self._item(tags='no-deploy-surface')
        self._signoff(item, author_email=ARCHITECTURE_EMAIL,
                      content='architecture sign-off: granted')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_marker_via_label_also_arms_class(self):
        """The marker may arrive as a label instead of a tag."""
        item = self._item(labels=['no-deploy-surface'])
        self._signoff(item)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    # ── (b) NO over-exemption: a merged head keeps the full PLS-675 path ────

    def test_merged_head_marker_stays_on_full_pls675_path(self):
        item = self._item(tags='no-deploy-surface', prs='merged')
        self._signoff(item)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_CORRELATED_DEPLOY])
        self.assertNotIn(WorkflowService._MISSING_SIGNOFF_RECEIPT, missing)

    def test_predicate_false_with_merged_head(self):
        item = self._item(tags='no-deploy-surface', prs='merged')
        self.assertFalse(WorkflowService._is_no_deploy_surface_class(item))

    # ── (c) missing proof denies with the explicit class code ───────────────

    def test_marker_without_signoff_denied_with_class_code(self):
        item = self._item(tags='no-deploy-surface')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_SIGNOFF_RECEIPT])

    def test_class_code_never_missing_merged_head(self):
        """The misleading merged-head code is never emitted for this class."""
        item = self._item(tags='no-deploy-surface')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertNotIn(WorkflowService._MISSING_MERGED_HEAD, missing)

    # ── authority isolation: who may stamp the receipt ──────────────────────

    def test_non_roster_signoff_projects_nothing(self):
        item = self._item(tags='no-deploy-surface')
        self._signoff(item, author_email=ENGINEERING_EMAIL)
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_SIGNOFF_RECEIPT])

    def test_automation_signoff_projects_nothing(self):
        item = self._item(tags='no-deploy-surface')
        self._signoff(item, author_email='system@pulse')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_SIGNOFF_RECEIPT])

    def test_non_approving_prose_projects_nothing(self):
        item = self._item(tags='no-deploy-surface')
        self._signoff(
            item,
            content='security review still pending, will follow up',
        )
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_SIGNOFF_RECEIPT])

    def test_missing_author_email_projects_nothing(self):
        item = self._item(tags='no-deploy-surface')
        self._signoff(item, author_email='')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_SIGNOFF_RECEIPT])

    # ── (d) focused-QA legs stay governed by PLAT-7600 ──────────────────────

    def test_focused_qa_leg_marker_does_not_enter_class(self):
        item = self._item(
            tags='no-deploy-surface',
            metadata={'focused_qa_origin_item_key': 'PLAT-1'},
        )
        self.assertFalse(WorkflowService._is_no_deploy_surface_class(item))
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertNotIn(WorkflowService._MISSING_SIGNOFF_RECEIPT, missing)

    # ── (e) no marker ⇒ byte-for-byte today's behavior ──────────────────────

    def test_no_marker_falls_through_unchanged(self):
        item = self._item(tags='user-facing')
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_MERGED_HEAD])

    def test_vocabulary_isolation_documentation_label_arms_nothing(self):
        """A focused-QA vocabulary token ('documentation') is NOT the marker —
        the new class is opt-in per task and never arms by class name."""
        item = self._item(labels=['documentation'])
        self.assertFalse(WorkflowService._is_no_deploy_surface_class(item))
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [WorkflowService._MISSING_MERGED_HEAD])
