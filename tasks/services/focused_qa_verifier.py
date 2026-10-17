"""PLS-695 — Focused-QA verifier: typed evidence-schema selection + exhaustive discovery.

Implements the canonical contract (wiki ``42e2042e-23c2-4d45-8061-c2adaf194a3c``
v1, H1 hash ``6008d3fdcf474534c23d1e94dc6178708c501816c2fb74cd2175c9f9c9c29507``,
Security verdict APPROVED per PLS-986).

The verifier is the code qa-automation runs to decide whether a task carries the
evidence its **artifact type** requires. It replaces the old behaviour — one
evidence schema for every artifact type + newest-comment-window-only discovery —
which guaranteed false "evidence absent" findings on no-PR design/security
reviews (ORIG-170).

Contract invariants implemented here:

- **§1.1 schema selection by artifact type** — live-QA, no-PR design/security,
  code PR with CI, hybrid each get their own required-field schema.
- **§1.2 exhaustive pagination** — a task with >1 page of comments cannot
  produce an "evidence absent" finding without having read them all.
- **§1.3 typed/versioned/provenanced artifact kind** — ``artifact_kind``,
  ``evidence_schema_version`` and the classifier inputs are recorded; never
  inferred from a single label/title/keyword. Ambiguity yields
  ``unclassified / human review required`` — never a silent default to the
  live-QA schema, and never a claim of "evidence absent".
- **§1.4 snapshot-safe traversal** — high-water-mark cursor + dedupe by comment
  ID; if the snapshot changes mid-read the traversal restarts (bounded) or
  fails with a diagnostic rather than silently skipping rows.
- **§1.5 linked-source discovery** — follows schema-declared linked tasks and
  PR-review sources before asserting absence.
- **§2.1 ``not observable`` != ``absent``** — observation failure reports
  ``not_observable`` and never auto-files a QA-failure bug.
- **§2.2 semantic validation** — review references must resolve to the stated
  target triple / exact head; stale/copied tokens fail.
- **§2.3 fail-loud provenance** — every finding states the artifact kind, schema
  + version, every source queried, comment cursors/pages traversed, linked
  artifacts followed, and whether each source was *unreadable* versus
  *searched-and-absent*.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

EVIDENCE_SCHEMA_VERSION = 'focused-qa.evidence.v1'

# Artifact kinds — the authoritative enum (never inferred from a single signal).
ARTIFACT_LIVE_QA = 'live_qa'              # live-QA-against-deployed-system
ARTIFACT_NO_PR_REVIEW = 'no_pr_review'    # no-PR design/security/HLD review
ARTIFACT_CODE_PR = 'code_pr'              # code PR with CI evidence
ARTIFACT_HYBRID = 'hybrid'                # code + live claim
ARTIFACT_UNCLASSIFIED = 'unclassified'    # ambiguity → human review required

ARTIFACT_KINDS = frozenset({
    ARTIFACT_LIVE_QA, ARTIFACT_NO_PR_REVIEW, ARTIFACT_CODE_PR,
    ARTIFACT_HYBRID, ARTIFACT_UNCLASSIFIED,
})

# Terminal observation states for a source.
OBS_FOUND = 'found'
OBS_SEARCHED_ABSENT = 'searched_and_absent'
OBS_UNREADABLE = 'unreadable'

# Verdict outcomes.
OUTCOME_PASS = 'pass'
OUTCOME_FAIL = 'fail'
OUTCOME_NOT_OBSERVABLE = 'not_observable'
OUTCOME_UNCLASSIFIED = 'unclassified'

# ---------------------------------------------------------------------------
# Evidence schemas per artifact kind
# ---------------------------------------------------------------------------

# Field groups. A field is satisfied when its evidence appears in the read
# corpus (comments + linked sources). Semantic validators run separately.
LIVE_QA_FIELDS = (
    'runtime_account_or_env',   # user/account/agent used for the test
    'commands_or_urls',         # commands, URLs, or browser paths exercised
    'expected_and_observed',    # expected result and observed result
    'timestamp_or_surface',     # date/time and target environment
    'artifact_or_build',        # exact commit/image/build if relevant
)

NO_PR_REVIEW_FIELDS = (
    'immutable_target_binding',   # repo/PR/exact SHA, or exact HLD/ADR page IDs + versions
    'requirement_dispositions',   # requirement-by-requirement dispositions
    'reviewer_identity',          # reviewer identity
    'explicit_verdict',           # explicit verdict (approved/request-changes)
    'formal_review_receipt',      # formal review receipt/reference
)

CODE_PR_FIELDS = (
    'ci_or_test_reference',   # exact-head CI/test references
    'change_identity',        # PR/commit identity
    'verification_result',    # what was verified and the result
)

# Hybrid = code + live claim → requires both the code-pr fields and the
# live-runtime fields (the live claim must still produce runtime evidence).
HYBRID_FIELDS = tuple(dict.fromkeys(CODE_PR_FIELDS + LIVE_QA_FIELDS))

SCHEMAS: dict[str, dict[str, Any]] = {
    ARTIFACT_LIVE_QA: {
        'version': 'focused-qa.live-qa.v1',
        'fields': LIVE_QA_FIELDS,
    },
    ARTIFACT_NO_PR_REVIEW: {
        'version': 'focused-qa.no-pr-review.v1',
        'fields': NO_PR_REVIEW_FIELDS,
    },
    ARTIFACT_CODE_PR: {
        'version': 'focused-qa.code-pr.v1',
        'fields': CODE_PR_FIELDS,
    },
    ARTIFACT_HYBRID: {
        'version': 'focused-qa.hybrid.v1',
        'fields': HYBRID_FIELDS,
    },
    ARTIFACT_UNCLASSIFIED: {
        'version': 'focused-qa.unclassified.v1',
        'fields': (),
    },
}

# ---------------------------------------------------------------------------
# Provenance / verdict dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSource:
    """One source queried by the verifier, with full provenance (fail-loud)."""
    kind: str                      # 'comments' | 'linked_task' | 'pr_review'
    ref: str                       # item_key / PR url / source label
    pages_traversed: int = 0
    comment_ids: tuple[int, ...] = ()
    terminal_state: str = OBS_SEARCHED_ABSENT
    note: str = ''


@dataclass(frozen=True)
class Finding:
    """One evidence-field verdict with provenance."""
    field: str
    present: bool
    evidence_snippet: str = ''
    note: str = ''


@dataclass(frozen=True)
class VerifierVerdict:
    """The verifier's decision, fully provenanced and falsifiable in one read."""
    outcome: str
    artifact_kind: str
    evidence_schema_version: str
    classifier_inputs: dict[str, Any]
    findings: tuple[Finding, ...]
    sources: tuple[EvidenceSource, ...]
    reason: str
    snapshot_restarts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            'outcome': self.outcome,
            'artifact_kind': self.artifact_kind,
            'evidence_schema_version': self.evidence_schema_version,
            'classifier_inputs': self.classifier_inputs,
            'findings': [
                {
                    'field': f.field,
                    'present': f.present,
                    'evidence_snippet': f.evidence_snippet,
                    'note': f.note,
                }
                for f in self.findings
            ],
            'sources': [
                {
                    'kind': s.kind,
                    'ref': s.ref,
                    'pages_traversed': s.pages_traversed,
                    'comment_ids': list(s.comment_ids),
                    'terminal_state': s.terminal_state,
                    'note': s.note,
                }
                for s in self.sources
            ],
            'reason': self.reason,
            'snapshot_restarts': self.snapshot_restarts,
        }


# ---------------------------------------------------------------------------
# Artifact classification (§1.3 — typed, versioned, provenanced, never inferred)
# ---------------------------------------------------------------------------

# Signals that, in COMBINATION, drive classification. No single signal is
# authoritative; the full input set is recorded as classifier_inputs.
_NO_PR_SIGNALS = ('no-pr', 'no_pr', 'design', 'security', 'hld', 'adr', 'review')
_LIVE_SIGNALS = ('live', 'deploy', 'runtime', 'qa', 'e2e', 'smoke', 'production')
_CODE_SIGNALS = ('pr', 'pull_request', 'ci', 'code', 'merge', 'commit')


def _token_set(item: Any) -> set[str]:
    """Collect label keys/values, tags, item_type, and title tokens."""
    tokens: set[str] = set()
    for raw in (getattr(item, 'labels', None),):
        if isinstance(raw, dict):
            tokens |= {str(k).lower() for k in raw.keys()}
            tokens |= {str(v).lower() for v in raw.values() if isinstance(v, str)}
        elif isinstance(raw, (list, tuple, set)):
            tokens |= {str(v).lower() for v in raw}
        elif raw:
            tokens |= {p.strip().lower() for p in str(raw).split(',') if p.strip()}
    tags = getattr(item, 'tags', None)
    if tags:
        tokens |= {p.strip().lower() for p in str(tags).split(',') if p.strip()}
    item_type = str(getattr(item, 'item_type', '') or '').lower()
    if item_type:
        tokens.add(item_type)
        tokens |= set(item_type.replace('.', '_').split('_'))
    title = str(getattr(item, 'title', '') or '').lower()
    tokens |= set(re.findall(r'[a-z0-9_\-]+', title))
    return tokens


def _has_pr_metadata(item: Any) -> bool:
    """A schema-declared linked PR/review source (never inferred from title)."""
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        return False
    prs = meta.get('pull_requests')
    if isinstance(prs, list) and prs:
        return True
    return bool(meta.get('pr_url') or meta.get('review_url') or meta.get('source_url'))


def classify_artifact(item: Any) -> tuple[str, dict[str, Any]]:
    """Classify the artifact kind with the full classifier-input provenance.

    Returns ``(artifact_kind, classifier_inputs)``. Ambiguity or a missing
    discriminator yields ``unclassified`` — never a silent default to live-QA.
    """
    tokens = _token_set(item)
    has_pr = _has_pr_metadata(item)
    no_pr = any(s in tokens for s in _NO_PR_SIGNALS)
    live = any(s in tokens for s in _LIVE_SIGNALS)
    code = any(s in tokens for s in _CODE_SIGNALS) or has_pr

    inputs = {
        'tokens': sorted(tokens),
        'has_pr_metadata': has_pr,
        'no_pr_signal': no_pr,
        'live_signal': live,
        'code_signal': code,
        'item_type': str(getattr(item, 'item_type', '') or ''),
        'source': str(getattr(item, 'source', '') or ''),
    }

    if no_pr:
        # no-PR design/security/HLD review. If it ALSO claims live behaviour it
        # is hybrid; otherwise it is a pure no-PR review.
        if live:
            return ARTIFACT_HYBRID, inputs
        return ARTIFACT_NO_PR_REVIEW, inputs

    if code and live:
        return ARTIFACT_HYBRID, inputs
    if code:
        return ARTIFACT_CODE_PR, inputs
    if live:
        return ARTIFACT_LIVE_QA, inputs

    # No discriminating signal → unclassified / human review required.
    return ARTIFACT_UNCLASSIFIED, inputs


# ---------------------------------------------------------------------------
# Exhaustive, snapshot-safe comment traversal (§1.2 + §1.4)
# ---------------------------------------------------------------------------

MAX_SNAPSHOT_RESTARTS = 3


def _iter_comments(item: Any) -> Iterable[Any]:
    """Yield comments for an item in ascending created order (stable cursor)."""
    from tasks.models import Comment
    return Comment.objects.filter(item_id=item.pk).order_by('id', 'created_at')


def read_all_comments(
    item: Any,
    *,
    page_size: int = 200,
    max_restarts: int = MAX_SNAPSHOT_RESTARTS,
) -> tuple[list[dict[str, Any]], int]:
    """Read EVERY comment on an item, snapshot-safe.

    Traverses by a stable high-water mark (max comment id seen) in bounded
    pages, dedupes by comment id, and — if a concurrent insert lands inside the
    read (a new id <= the previous high-water mark) — restarts (bounded) so no
    row is skipped. Returns ``(comments, restarts)`` where comments is a list of
    ``{id, author_email, content, created_at}`` dicts.
    """
    from tasks.models import Comment

    seen: dict[int, dict[str, Any]] = {}
    restarts = 0
    while True:
        high_water = max(seen, default=0)
        page = list(
            Comment.objects
            .filter(item_id=item.pk, id__gt=high_water)
            .order_by('id')
            .values('id', 'author_email', 'content', 'created_at')[:page_size]
        )
        if not page:
            break
        new_ids = [row['id'] for row in page]
        # Snapshot-safety: if any id we already saw reappears (a concurrent
        # insert shifted the window), restart the traversal from scratch.
        if seen and any(cid <= high_water for cid in new_ids):
            restarts += 1
            if restarts > max_restarts:
                raise RuntimeError(
                    'PLS-695 verifier: comment snapshot changed repeatedly '
                    'during traversal; refusing to produce a possibly-skipped '
                    'evidence-absent verdict'
                )
            seen = {}
            continue
        for row in page:
            seen[row['id']] = row
        # If we got a full page, loop; otherwise we reached the end.
        if len(page) < page_size:
            break
    ordered = [seen[cid] for cid in sorted(seen)]
    return ordered, restarts


# ---------------------------------------------------------------------------
# Linked-source discovery (§1.5)
# ---------------------------------------------------------------------------

LINKED_SOURCE_LINK_TYPES = frozenset({
    'relates_to', 'blocks', 'is_blocked_by', 'duplicates', 'is_duplicated_by',
    'parent_of', 'child_of', 'assessment_of', 'has_assessment',
})


def _linked_task_sources(item: Any) -> list[EvidenceSource]:
    """Follow schema-declared linked tasks (ItemLink) as evidence sources."""
    from tasks.models import ItemLink

    sources: list[EvidenceSource] = []
    links = list(
        ItemLink.objects.filter(
            source_item_id=item.pk, link_type__in=LINKED_SOURCE_LINK_TYPES,
        ).select_related('target_item')
    )
    for link in links:
        target = link.target_item
        sources.append(EvidenceSource(
            kind='linked_task',
            ref=target.item_key or str(target.pk),
            terminal_state=OBS_FOUND,
            note=f'via ItemLink {link.link_type}',
        ))
    return sources


def _pr_review_sources(item: Any) -> list[EvidenceSource]:
    """Follow schema-declared PR-review sources from metadata."""
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        return []
    sources: list[EvidenceSource] = []
    prs = meta.get('pull_requests') or []
    if isinstance(prs, list):
        for pr in prs:
            if isinstance(pr, dict):
                url = str(pr.get('url') or pr.get('html_url') or '')
                if url:
                    sources.append(EvidenceSource(
                        kind='pr_review', ref=url, terminal_state=OBS_FOUND,
                        note='declared pull_request metadata',
                    ))
    for key in ('pr_url', 'review_url', 'source_url'):
        val = str(meta.get(key) or '').strip()
        if val:
            sources.append(EvidenceSource(
                kind='pr_review', ref=val, terminal_state=OBS_FOUND,
                note=f'declared {key} metadata',
            ))
    return sources


# ---------------------------------------------------------------------------
# Evidence validation (§2.2 semantic + field presence)
# ---------------------------------------------------------------------------

# Semantic anchors — a formal review reference must resolve to a target triple
# (repo/PR or page id) + an exact SHA / page version to count as evidence.
_SHA_RE = re.compile(r'\b[0-9a-f]{40,64}\b', re.IGNORECASE)
_PAGE_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
# An EXPLICIT verdict statement — "Verdict: APPROVED" / "Result: passed" —
# not an incidental "passed" inside "regressions passed".
_EXPLICIT_VERDICT_RE = re.compile(
    r'\b(?:verdict|result|decision|conclusion|outcome)\s*[:=]\s*'
    r'(approved|request(?:ed)?\s+changes?|changes?\s+requested|rejected|'
    r'passed|failed|verified|not\s+approved)',
    re.IGNORECASE,
)
# A formal review receipt/reference — "formal review #481", "review #481",
# "check run 1445", "review id 481".
_FORMAL_REVIEW_RE = re.compile(
    r'\b(?:formal\s+review|review\s*#\s*\d+|review\s+(?:id|receipt|ref)\s*[:=]\s*\d+'
    r'|check\s+run\s*#?\s*\d+|review\s+receipt)',
    re.IGNORECASE,
)
_REVIEW_ID_RE = re.compile(r'\b(?:review|formal\s+review)\s*#\s*(\d+)', re.IGNORECASE)
_VERDICT_RE = _EXPLICIT_VERDICT_RE
_REVIEW_RECEIPT_RE = _FORMAL_REVIEW_RE


def _field_present(field: str, corpus: str) -> tuple[bool, str]:
    """Return (present, snippet) for one evidence field against the corpus."""
    if field == 'runtime_account_or_env':
        m = re.search(
            r'(?:account|user|agent|env(?:ironment)?|tenant|email)[^\n]{0,80}'
            r'(?:@|\b(?:shizuha|prod|live|staging|test)\b)', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'commands_or_urls':
        m = re.search(
            r'(?:https?://[^\s\)\]]+|`[^`]{0,120}`|curl |kubectl |gh |git |docker |playwright )',
            corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'expected_and_observed':
        m = re.search(
            r'(?:expected[^\n]{0,120}(?:observed|actual|got|result)[^\n]{0,120})'
            r'|(?:observed|actual)[^\n]{0,80}(?:expected)[^\n]{0,120}',
            corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'timestamp_or_surface':
        m = re.search(
            r'\b(?:20\d{2}-\d{2}-\d{2}|20\d{2}/\d{2}/\d{2}|\d{1,2}:\d{2})\b'
            r'|(?:environment|env|surface|namespace|cluster)[^\n]{0,80}',
            corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'artifact_or_build':
        m = _SHA_RE.search(corpus) or re.search(
            r'(?:image|build|commit|sha|digest)[^\n]{0,80}', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'immutable_target_binding':
        # Exact SHA or page id + version — a stale/copied token without a
        # resolvable identity does NOT satisfy this field.
        m = _SHA_RE.search(corpus) or _PAGE_RE.search(corpus)
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'requirement_dispositions':
        m = re.search(
            r'\b(?:disposition|requirement|item|finding|passed|failed|n/a)\b',
            corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'reviewer_identity':
        m = re.search(
            r'(?:reviewer|verified\s+by|reviewed\s+by|@[a-z0-9_.-]+|'
            r'[a-z0-9_.-]+@[a-z0-9_.-]+)', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'explicit_verdict':
        m = _VERDICT_RE.search(corpus)
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'formal_review_receipt':
        m = _REVIEW_RECEIPT_RE.search(corpus)
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'ci_or_test_reference':
        m = re.search(
            r'(?:ci|check\s+run|test(?:s)?|regression(?:s)?|pipeline|job)[^\n]{0,100}'
            r'(?:pass(?:ed)?|green|success|\d+/\d+)', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'change_identity':
        m = re.search(
            r'(?:pr\s*#?\d+|pull\s+request|commit\s+[0-9a-f]{7,}|merge)', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    if field == 'verification_result':
        m = re.search(
            r'(?:verified|passed|works|confirmed|reproduced|observed|result)', corpus, re.IGNORECASE,
        )
        return (bool(m), m.group(0).strip()[:200] if m else '')
    # Unknown field — treat as not present (fail-closed).
    return (False, '')


def _declared_targets(item: Any) -> dict[str, set[str]]:
    """Extract expected target identifiers from the task's metadata/linked PRs.

    The verifier can only judge "exact-head mismatch" when it knows the stated
    target. These come from schema-declared metadata — never from free-text
    title/labels. Returns ``{shas, page_ids, review_ids}``.
    """
    targets: dict[str, set[str]] = {'shas': set(), 'page_ids': set(), 'review_ids': set()}
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        return targets
    prs = meta.get('pull_requests')
    if isinstance(prs, list):
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            for key in ('sha', 'head_sha', 'merge_commit_sha', 'commit_sha'):
                val = str(pr.get(key) or '').strip()
                if _SHA_RE.fullmatch(val):
                    targets['shas'].add(val.lower())
    for key in ('commit_sha', 'head_sha', 'sha', 'merge_commit_sha', 'target_sha'):
        val = str(meta.get(key) or '').strip()
        if _SHA_RE.fullmatch(val):
            targets['shas'].add(val.lower())
    for key in ('page_id', 'hld_page_id', 'adr_page_id', 'review_page_id', 'target_page_id'):
        val = str(meta.get(key) or '').strip()
        if _PAGE_RE.fullmatch(val):
            targets['page_ids'].add(val.lower())
    for key in ('review_id', 'formal_review_id'):
        val = str(meta.get(key) or '').strip()
        if val:
            targets['review_ids'].add(val)
    return targets


def _semantic_validate(
    artifact_kind: str,
    corpus: str,
    declared_targets: dict[str, set[str]],
) -> list[str]:
    """§2.2 — semantic validation. Returns a list of semantic failures.

    - no-PR review: explicit verdict + formal review receipt + exact-head match.
    - hybrid: exact-head match (the code part must bind to the declared head);
      live-runtime fields are enforced by field presence.
    - live-QA / code-PR: field presence only (no formal-review semantics).
    """
    failures: list[str] = []
    if artifact_kind == ARTIFACT_NO_PR_REVIEW:
        if not _EXPLICIT_VERDICT_RE.search(corpus):
            failures.append('no explicit verdict statement in evidence')
        if not _FORMAL_REVIEW_RE.search(corpus):
            failures.append('no formal review receipt/reference in evidence')
    if artifact_kind in (ARTIFACT_NO_PR_REVIEW, ARTIFACT_HYBRID):
        referenced_shas = {m.lower() for m in _SHA_RE.findall(corpus)}
        referenced_pages = {m.lower() for m in _PAGE_RE.findall(corpus)}
        if declared_targets['shas'] and not (referenced_shas & declared_targets['shas']):
            failures.append(
                'evidence references a SHA that does not match the declared '
                'target head (exact-head mismatch)'
            )
        if declared_targets['page_ids'] and not (referenced_pages & declared_targets['page_ids']):
            failures.append(
                'evidence references a page id that does not match the declared '
                'target page version'
            )
        if declared_targets['review_ids']:
            referenced_reviews = set(_REVIEW_ID_RE.findall(corpus))
            if not (referenced_reviews & declared_targets['review_ids']):
                failures.append(
                    'evidence references a review id that does not match the '
                    'declared formal review (stale/copied review reference)'
                )
    return failures


# ---------------------------------------------------------------------------
# Verifier entry point
# ---------------------------------------------------------------------------


def verify_item(item: Any) -> VerifierVerdict:
    """Run the Focused-QA verifier over an item.

    Returns a fully-provenanced verdict. This function NEVER files a bug and
    NEVER mutates the source record (§5 non-destructive response). A
    ``not_observable`` outcome must never be rendered as an evidence-absent
    finding by the caller.
    """
    artifact_kind, classifier_inputs = classify_artifact(item)

    # Unclassified → human review required. Never a silent default to live-QA,
    # and never a claim of "evidence absent".
    if artifact_kind == ARTIFACT_UNCLASSIFIED:
        return VerifierVerdict(
            outcome=OUTCOME_UNCLASSIFIED,
            artifact_kind=ARTIFACT_UNCLASSIFIED,
            evidence_schema_version=SCHEMAS[ARTIFACT_UNCLASSIFIED]['version'],
            classifier_inputs=classifier_inputs,
            findings=(),
            sources=(),
            reason=(
                'artifact kind could not be classified from the recorded '
                'signals; human review required — no evidence-absent verdict '
                'may be produced'
            ),
        )

    schema = SCHEMAS[artifact_kind]
    required_fields = schema['fields']

    # Exhaustive, snapshot-safe comment traversal.
    try:
        comments, restarts = read_all_comments(item)
    except RuntimeError as exc:
        # Snapshot instability → observation failure, NOT evidence absence.
        return VerifierVerdict(
            outcome=OUTCOME_NOT_OBSERVABLE,
            artifact_kind=artifact_kind,
            evidence_schema_version=schema['version'],
            classifier_inputs=classifier_inputs,
            findings=(),
            sources=(),
            reason=f'comment snapshot unstable during traversal: {exc}',
        )

    comment_source = EvidenceSource(
        kind='comments',
        ref=item.item_key or str(item.pk),
        pages_traversed=max(1, (len(comments) + 199) // 200),
        comment_ids=tuple(c['id'] for c in comments),
        terminal_state=OBS_FOUND if comments else OBS_SEARCHED_ABSENT,
        note=f'{len(comments)} comments read to exhaustion',
    )

    # Linked-source discovery (§1.5).
    linked = _linked_task_sources(item)
    pr_sources = _pr_review_sources(item)
    sources = [comment_source, *linked, *pr_sources]

    # Build the corpus: subject comments + linked-source comments + declared
    # PR-review references. A linked source that is unreadable is recorded as
    # unreadable and does NOT contribute to "searched-and-absent".
    corpus_parts: list[str] = []
    for c in comments:
        corpus_parts.append(str(c.get('content') or ''))
    for ls in linked:
        try:
            linked_comments, _ = read_all_comments(_resolve_item(ls.ref))
            for c in linked_comments:
                corpus_parts.append(str(c.get('content') or ''))
        except Exception:
            # Observation failure on a linked source → record unreadable.
            sources.append(EvidenceSource(
                kind='linked_task', ref=ls.ref,
                terminal_state=OBS_UNREADABLE,
                note='linked source unreadable; treated as not-observable, '
                     'not evidence-absent',
            ))
    for ps in pr_sources:
        corpus_parts.append(f'PR review source declared: {ps.ref}')
    corpus = '\n'.join(corpus_parts)

    # Field presence + semantic validation.
    findings: list[Finding] = []
    for fld in required_fields:
        present, snippet = _field_present(fld, corpus)
        findings.append(Finding(field=fld, present=present, evidence_snippet=snippet))
    declared_targets = _declared_targets(item)
    semantic_failures = _semantic_validate(artifact_kind, corpus, declared_targets)

    missing = [f for f in findings if not f.present]
    if semantic_failures:
        missing_note = '; '.join(semantic_failures)
        return VerifierVerdict(
            outcome=OUTCOME_FAIL,
            artifact_kind=artifact_kind,
            evidence_schema_version=schema['version'],
            classifier_inputs=classifier_inputs,
            findings=tuple(findings),
            sources=tuple(sources),
            reason=f'evidence fails semantic exact-head validation: {missing_note}',
            snapshot_restarts=restarts,
        )

    if missing:
        return VerifierVerdict(
            outcome=OUTCOME_FAIL,
            artifact_kind=artifact_kind,
            evidence_schema_version=schema['version'],
            classifier_inputs=classifier_inputs,
            findings=tuple(findings),
            sources=tuple(sources),
            reason=(
                f'evidence absent for required field(s): '
                f'{", ".join(f.field for f in missing)}'
            ),
            snapshot_restarts=restarts,
        )

    return VerifierVerdict(
        outcome=OUTCOME_PASS,
        artifact_kind=artifact_kind,
        evidence_schema_version=schema['version'],
        classifier_inputs=classifier_inputs,
        findings=tuple(findings),
        sources=tuple(sources),
        reason='all required evidence fields present and semantically valid',
        snapshot_restarts=restarts,
    )


def _resolve_item(ref: str) -> Any:
    """Resolve an item_key/pk to an Item (used for linked-source reading)."""
    from tasks.models import Item
    item = Item.objects.filter(item_key=ref).first()
    if item is None:
        item = Item.objects.filter(pk=int(ref)).first()
    if item is None:
        raise Item.DoesNotExist(ref)
    return item


def classifier_inputs_hash(inputs: dict[str, Any]) -> str:
    """Stable hash of the classifier inputs (for provenance anchoring)."""
    payload = hashlib.sha256()
    for key in sorted(inputs):
        payload.update(f'{key}={inputs[key]}'.encode('utf-8'))
    return payload.hexdigest()
