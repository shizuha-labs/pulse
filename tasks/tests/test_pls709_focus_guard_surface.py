"""PLS-709: the comment path must not borrow the transition path's reason."""
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class _Stub:
    def __init__(self, key, pk=1, status='todo'):
        self.item_key = key
        self.pk = pk
        self.status = status


class FocusGuardDiagnosticSurfaceTests(SimpleTestCase):
    """The two surfaces share a code and must not share a cause."""

    def _diag(self, **kw):
        with patch.object(WorkflowService, '_queue_snapshot_cache_key', return_value=None):
            return WorkflowService._focus_guard_diagnostic(
                _Stub('PLAT-5097', pk=1), 'a@b.c', _Stub('MAIL-85', pk=2), **kw
            )

    def test_transition_surface_wording_is_unchanged(self):
        """Existing consumers key on this text; it must not drift."""
        code, details = self._diag()
        self.assertEqual(code, 'BLOCKER_FOCUS_REQUIRED')
        self.assertIn('Cannot start/resume PLAT-5097', details['detail'])
        self.assertEqual(details['guard_surface'], 'transition')

    def test_comment_surface_does_not_claim_a_transition(self):
        """The defect: no transition occurs on the comment path."""
        code, details = self._diag(surface='comment')
        self.assertEqual(code, 'BLOCKER_FOCUS_REQUIRED')
        self.assertNotIn('start/resume', details['detail'])
        self.assertIn('Comment on PLAT-5097 refused', details['detail'])
        self.assertIn('No transition was attempted', details['detail'])
        self.assertIn('MAIL-85', details['detail'])

    def test_the_two_surfaces_are_machine_distinguishable(self):
        """From the response alone, not by parsing prose."""
        _, transition = self._diag()
        _, comment = self._diag(surface='comment')
        self.assertNotEqual(transition['guard_surface'], comment['guard_surface'])
        self.assertNotEqual(transition['detail'], comment['detail'])
        # Same code on purpose: callers and tests key on it.
        self.assertEqual(transition['code'], comment['code'])


class FocusGuardCallSiteSurfaceTests(SimpleTestCase):
    """Every comment-surface CALLER must declare it.

    The tests above prove the helper renders the right reason when told the
    surface. They cannot catch a caller that never tells it -- which is exactly
    what happened: the first cut of PLS-709 patched CommentViewSet and left
    ExternalMcpCallView._add_comment defaulting to 'transition', so external
    MCP pulse_add_comment still returned 409 "Cannot start/resume". A helper
    test is green on that bug.

    Invariant asserted here: BOTH _focus_guard_diagnostic call sites in
    views.py are comment surfaces (CommentViewSet.perform_create and
    ExternalMcpCallView._add_comment); the transition caller lives in
    workflow_service.py. So no call in views.py may omit surface='comment'.
    """

    CALL = '_focus_guard_diagnostic('

    def _views_source(self):
        from pathlib import Path
        import tasks.views as views_module
        return Path(views_module.__file__).read_text(encoding='utf-8')

    def _call_sites(self, source):
        """Return (line_no, argument_text) for each call, paren-balanced."""
        sites = []
        idx = source.find(self.CALL)
        while idx != -1:
            line_no = source.count('\n', 0, idx) + 1
            depth, j = 0, idx + len(self.CALL) - 1
            while j < len(source):
                if source[j] == '(':
                    depth += 1
                elif source[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            sites.append((line_no, source[idx:j + 1]))
            idx = source.find(self.CALL, j)
        return sites

    def test_closed_set_of_call_sites_is_what_we_think(self):
        """A positive control: if this count drifts, the invariant below is stale."""
        sites = self._call_sites(self._views_source())
        self.assertEqual(
            len(sites), 2,
            f'views.py _focus_guard_diagnostic call sites changed: {[s[0] for s in sites]}. '
            'A new one must be classified as comment or transition before this passes.',
        )

    def test_every_views_call_site_declares_the_comment_surface(self):
        for line_no, call in self._call_sites(self._views_source()):
            with self.subTest(line=line_no):
                self.assertIn(
                    "surface='comment'", call,
                    f"views.py:{line_no} omits surface='comment', so it renders the "
                    'transition reason ("Cannot start/resume") for a comment.',
                )
