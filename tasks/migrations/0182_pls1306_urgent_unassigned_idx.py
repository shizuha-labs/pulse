# PLS-1306: covering index for the urgent-unassigned gauge exposition.
#
# tasks/services/urgent_unassigned_metrics.py filters priority IN (urgent,
# high) AND status IN (todo, open, needs_triage) AND (assignee_email IS NULL
# OR '') and orders by created_at. No existing index leads with those equality
# columns, so the collection seq-scanned tasks_task, overran the
# pulse-recurring-invariants ServiceMonitor's 10s scrapeTimeout, and
# Prometheus kept each pod's last successful sample for tens of minutes —
# PulseUrgentOperatorTaskStale fired three times on 09-19 from three pods on
# frozen values (689.69h gauge vs 690.48h live) for a backlog that had already
# been routed. Column order: equality predicates first, assignee_email third
# (IS NULL is btree-servable), created_at last so the oldest-item walk is an
# ordered index scan.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0181_pls527_operator_wait_evidence'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='item',
            index=models.Index(
                fields=['priority', 'status', 'assignee_email', 'created_at'],
                name='pls1306_urgent_unassigned_idx',
            ),
        ),
    ]
