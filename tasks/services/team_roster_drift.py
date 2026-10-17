"""Detect required-seat shrinkage in the exact-org TeamRoster authority.

ADR-0041 keeps ``TeamRoster`` / ``team_roster_compat`` authoritative for live
assignment until a lane is materialized in ``RoutingTeamBinding``.  The policy
below is comparison input only: it must never be unioned into resolver output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from tasks.models import TeamRoster


# PLS-833 Architecture ruling, repaired live by PLS-865.  Keep this deliberately
# small and reviewable: deleting one of these seats must make CI and the live-read
# command fail loudly instead of silently reducing Merge capacity.
EXPECTED_TEAM_ROSTER_MEMBERS = {
    'shizuha:merge': frozenset({
        'aoi@shizuha.com',
        'haru@shizuha.com',
        'kei@shizuha.com',
        'ryo@shizuha.com',
    }),
}

EXPECTED_TEAM_ROSTER_ORGANIZATION_IDS = {
    'shizuha:merge': 1,
}


def _normalize_members(members) -> frozenset[str]:
    return frozenset(
        value
        for member in members or ()
        if isinstance(member, str)
        if (value := member.strip().lower())
    )


@dataclass(frozen=True)
class TeamRosterDriftSignal:
    canonical_key: str
    organization_id: int
    source: str
    source_state: str
    source_version: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def drifted(self) -> bool:
        return bool(self.missing)

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            'status': 'drift' if self.drifted else 'ok',
        }


def compare_team_roster_members(
    canonical_key: str,
    actual_members,
    *,
    source_state: str = 'active',
    source_version: str = 'fixture',
) -> TeamRosterDriftSignal:
    """Compare one authority snapshot with its pinned required-seat floor."""
    expected = EXPECTED_TEAM_ROSTER_MEMBERS[canonical_key]
    actual = _normalize_members(actual_members)
    return TeamRosterDriftSignal(
        canonical_key=canonical_key,
        organization_id=EXPECTED_TEAM_ROSTER_ORGANIZATION_IDS[canonical_key],
        source='team_roster_compat',
        source_state=source_state,
        source_version=source_version,
        expected=tuple(sorted(expected)),
        actual=tuple(sorted(actual)),
        missing=tuple(sorted(expected - actual)),
        unexpected=tuple(sorted(actual - expected)),
    )


def read_team_roster_drift_signals() -> tuple[TeamRosterDriftSignal, ...]:
    """Read each pinned exact-org row through the live compatibility boundary."""
    signals = []
    for canonical_key in sorted(EXPECTED_TEAM_ROSTER_MEMBERS):
        organization_slug, team_name = canonical_key.split(':', 1)
        organization_id = EXPECTED_TEAM_ROSTER_ORGANIZATION_IDS[canonical_key]
        row = TeamRoster.objects.filter(
            organization_slug=organization_slug,
            organization_id=organization_id,
            team_name=team_name,
        ).first()

        if row is None:
            state = 'missing'
            members = ()
            version = 'row:missing'
        else:
            state = 'active' if row.is_active else 'inactive'
            members = (
                TeamRoster._flat_member_emails(row.tiers, row.junior_emails)
                if row.is_active else ()
            )
            version = (
                f'row:{row.pk};version:{row.version};'
                f'updated:{row.updated_at.isoformat()}'
            )

        signals.append(compare_team_roster_members(
            canonical_key,
            members,
            source_state=state,
            source_version=version,
        ))
    return tuple(signals)
