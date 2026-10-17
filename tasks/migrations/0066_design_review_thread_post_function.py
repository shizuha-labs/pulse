"""PLAT-967 (HLD PLAT-963 §2.4) — Design-Review Thread Ritual.

Attach a `connect_group_post` post-function to every "Submit for Architecture
Review" transition so that, when a design/HLD/ADR task enters Architecture
Review, a §2.3 Design-Review message is posted to the Design Review Connect
group. The post is scoped to design/HLD/ADR items (class_filter) and is
non-blocking/advisory — it never gates the formal review.

The target conversation is resolved from settings.DESIGN_REVIEW_CONVERSATION_ID
at post time (single source of truth), so the post-function carries no hardcoded
id. Idempotent (won't double-add) and reversible (removes only what it added).
"""
import json

from django.db import migrations

_PF = {'type': 'connect_group_post', 'class_filter': 'design_review'}


def _load_pfs(transition):
    pfs = transition.post_functions
    if isinstance(pfs, str):
        try:
            pfs = json.loads(pfs or '[]')
        except (ValueError, json.JSONDecodeError):
            pfs = []
    return list(pfs or [])


def attach(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for t in WorkflowTransition.objects.filter(name='Submit for Architecture Review'):
        pfs = _load_pfs(t)
        if any(isinstance(p, dict) and p.get('type') == 'connect_group_post' for p in pfs):
            continue
        pfs.append(dict(_PF))
        t.post_functions = pfs
        t.save(update_fields=['post_functions'])


def detach(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for t in WorkflowTransition.objects.filter(name='Submit for Architecture Review'):
        pfs = _load_pfs(t)
        new_pfs = [p for p in pfs
                   if not (isinstance(p, dict) and p.get('type') == 'connect_group_post')]
        if len(new_pfs) != len(pfs):
            t.post_functions = new_pfs
            t.save(update_fields=['post_functions'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0065_critical_team_failover_tiers'),
    ]

    operations = [
        migrations.RunPython(attach, detach),
    ]
