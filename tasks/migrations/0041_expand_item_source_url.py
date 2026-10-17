# Expand Item.source_url for long Prometheus Alertmanager generatorURL values

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0040_add_pr_activity_actions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='item',
            name='source_url',
            field=models.URLField(blank=True, help_text='Link to source system', max_length=2048, null=True),
        ),
    ]
