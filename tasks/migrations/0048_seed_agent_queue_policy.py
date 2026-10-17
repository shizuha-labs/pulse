"""Seed agent queue policy into workflow knowledge articles."""

from django.db import migrations


QUEUE_ARTICLE_TITLE = 'Agent Queue Policy — Priority then FIFO'
QUEUE_ARTICLE = {
    'type': 'inline',
    'title': QUEUE_ARTICLE_TITLE,
    'content': (
        'When choosing assigned work, use Pulse queue order: ready work first, '
        'then highest priority (`urgent` > `high` > `normal`/`medium` > `low`), '
        'then FIFO by `created_at` inside that priority bucket. Work the first '
        'task you can move. If it is misrouted, blocked, operator-only, or '
        'outside your pod, triage/forward it the same turn and continue to the '
        'next eligible task. Do not cherry-pick newer same-priority tasks.'
    ),
}

START_ARTICLE_TITLE = 'Before Start Progress — Respect Queue Order'
START_ARTICLE = {
    'type': 'inline',
    'title': START_ARTICLE_TITLE,
    'content': (
        'Start this task only if it is the first movable task in your '
        '`pulse_get_my_tasks` queue, or if a direct operator/agent handoff '
        'explicitly told you to handle this item now. Otherwise return to the '
        'higher-priority/older eligible task first. If this task cannot be '
        'moved by you, comment and reassign/transition it instead of holding it.'
    ),
}


def seed_agent_queue_policy(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.filter(is_active=True):
        kb = list(workflow.knowledge_base or [])
        kb = [article for article in kb if article.get('title') != QUEUE_ARTICLE_TITLE]
        kb.append(QUEUE_ARTICLE)
        workflow.knowledge_base = kb
        workflow.save(update_fields=['knowledge_base', 'updated_at'])

    start_transitions = (
        WorkflowTransition.objects
        .filter(to_status='in_progress')
        .filter(name__iregex=r'start|progress|begin|work')
    )
    for transition in start_transitions:
        articles = list(transition.knowledge_articles or [])
        articles = [
            article
            for article in articles
            if article.get('title') != START_ARTICLE_TITLE
        ]
        articles.insert(0, START_ARTICLE)
        transition.knowledge_articles = articles
        transition.save(update_fields=['knowledge_articles', 'updated_at'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0047_seed_blocker_paths_all_workflows'),
    ]

    operations = [
        migrations.RunPython(seed_agent_queue_policy, noop),
    ]
