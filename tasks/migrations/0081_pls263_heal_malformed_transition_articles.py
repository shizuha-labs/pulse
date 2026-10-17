"""PLS-263: heal malformed knowledge-article rows (PLS-194 / PLS-224 class).

A ``knowledge_articles`` (or workflow ``knowledge_base``) JSONField stored as a
list whose *elements* are strings — a JSON-encoded article string or bare text —
crashed the MCP transition read boundary with ``'str' object has no attribute
'get'`` (the security-finding transition PLAT-1610 reached). The read boundary
is now fail-soft (mcp-server ``_safe_json_list`` item-coercion), but the stored
rows are still malformed, so this migration re-stores them as proper JSON lists
of article dicts.

Self-contained normalizer (does not import evolving app helpers, per migration
best practice): coerce the outer container to a list, then parse string items
and drop non-dict items — mirroring ``normalize_knowledge_articles``. Only rows
whose stored value actually changes are written, so clean rows are untouched.
"""
import json

from django.db import migrations


def _normalize_article_list(value):
    """Return ``value`` as a clean list of article dicts, or None if it is
    already clean / empty / not a heal target."""
    # Empty / unset: nothing to heal.
    if value in (None, "", []):
        return None

    # Outer container -> list.
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []  # unparseable string -> a proper empty list
        if isinstance(raw, str):  # double-encoded
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
    if not isinstance(raw, list):
        return []

    # Item coercion: parse string items, keep dicts, drop the rest.
    normalized = []
    for item in raw:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def heal(apps, schema_editor):
    WorkflowTransition = apps.get_model("tasks", "WorkflowTransition")
    Workflow = apps.get_model("tasks", "Workflow")

    for model, field in (
        (WorkflowTransition, "knowledge_articles"),
        (Workflow, "knowledge_base"),
    ):
        for row in model.objects.all().iterator():
            stored = getattr(row, field, None)
            cleaned = _normalize_article_list(stored)
            if cleaned is None:
                continue  # empty / not a heal target
            if cleaned == stored:
                continue  # already clean (native list of dicts)
            setattr(row, field, cleaned)
            row.save(update_fields=[field])


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0080_pls238_qa_cadence_rotation"),
    ]

    operations = [
        migrations.RunPython(heal, migrations.RunPython.noop),
    ]
