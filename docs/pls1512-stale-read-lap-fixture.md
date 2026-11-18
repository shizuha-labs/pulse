# PLS-1512 stale-read lap fixture

Content carrier for the PLS-1512 fixture lap (PLS-1395 ask #4 acceptance):
an out-of-band Origin-API merge whose post-merge state must be visible to the
Pulse merge-precondition checker (PLS-466 freshness fix, carrier #848 at ca034bcd)
within 60s.

Lap shape: official head-exact approval (non-author) -> API merge -> PR-gated parent
transition within 60s -> receipt on PLS-1512, linked to PLS-1445 (item 42153).
Witness: hydrogen (PLS-1445 assignee). Verdict: nami settles; Zen adjudicates.
