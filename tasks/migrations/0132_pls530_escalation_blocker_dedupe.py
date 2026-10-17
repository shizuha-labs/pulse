from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0131_pls478_comment_provenance"),
    ]

    operations = [
        migrations.AddField(
            model_name="escalationroutingdisposition",
            name="blocker_dependency_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="escalationroutingdisposition",
            name="blocker_result",
            field=models.CharField(blank=True, db_index=True, default="", max_length=20),
        ),
        migrations.CreateModel(
            name="EscalationBlockerDependency",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("organization_id", models.PositiveIntegerField(db_index=True)),
                ("dependency_key", models.CharField(max_length=64)),
                ("target_team", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "blocker",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="canonical_escalation_dependencies",
                        to="tasks.item",
                    ),
                ),
            ],
            options={
                "db_table": "tasks_escalation_blocker_dependency",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "dependency_key"),
                        name="uniq_escalation_blocker_dependency",
                    )
                ],
            },
        ),
    ]
