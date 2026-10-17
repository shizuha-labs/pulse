from django.db import migrations


class Migration(migrations.Migration):
    """Merge the two migration leaves that appeared when PLS-228
    (0080_pls228_notification_delegate, off 0079) merged in parallel with the
    0080_pls238_qa_cadence_rotation -> 0081_pls263_heal_malformed_transition_articles
    chain. Both branch from 0079, so `migrate`/`makemigrations --check` reported
    "multiple leaf nodes" and blocked the deploy gate. Empty merge migration —
    no schema changes, just unifies the graph to a single leaf.
    """

    dependencies = [
        ("tasks", "0080_pls228_notification_delegate"),
        ("tasks", "0081_pls263_heal_malformed_transition_articles"),
    ]

    operations = []
