from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0063_backlog_status_category'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='item',
            constraint=models.UniqueConstraint(
                fields=('source', 'source_id'),
                condition=(
                    models.Q(source='qa_cadence_sweep') &
                    models.Q(source_id__isnull=False) &
                    ~models.Q(status__in=[
                        'done', 'completed', 'closed', 'cancelled', 'canceled',
                        'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                        'failed',
                    ])
                ),
                name='uniq_active_qa_cadence_source',
            ),
        ),
    ]
