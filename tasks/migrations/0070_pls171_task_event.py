# PLS-171: append-only TaskEvent delta log for live frontend updates (Slice 1).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0069_seed_devops_escalation_all_workflows"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaskEvent",
            fields=[
                ("seq", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("item_created", "Item created"),
                            ("item_updated", "Item updated"),
                            ("item_deleted", "Item deleted"),
                            ("comment_added", "Comment added"),
                            ("link_changed", "Link changed"),
                        ],
                        max_length=32,
                    ),
                ),
                ("org_id", models.IntegerField(blank=True, db_index=True, null=True)),
                (
                    "project_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                (
                    "item_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                ("item_key", models.CharField(blank=True, default="", max_length=64)),
                ("actor_user_id", models.IntegerField(blank=True, null=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "db_table": "tasks_task_event",
                "ordering": ["seq"],
                "indexes": [
                    models.Index(
                        fields=["project_id", "seq"],
                        name="tasks_task__project_e4e1a8_idx",
                    ),
                    models.Index(
                        fields=["org_id", "seq"], name="tasks_task__org_id_ee01b0_idx"
                    ),
                    models.Index(
                        fields=["created_at"], name="tasks_task__created_876303_idx"
                    ),
                ],
            },
        ),
    ]
