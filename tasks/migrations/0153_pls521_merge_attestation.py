import django.db.models.deletion
import django.utils.timezone
import uuid

from django.db import migrations, models


def require_pre_activation_empty(apps, schema_editor):
    Manifest = apps.get_model('tasks', 'VerificationRequirementManifest')
    if Manifest.objects.exists():
        raise RuntimeError(
            'PLS-521 merge-attestation migration requires an empty pre-activation manifest table'
        )


class Migration(migrations.Migration):
    """PLS-521: bind each manifest to provider-attested PR merge identity."""

    dependencies = [
        ('tasks', '0152_pls521_manifest_cluster_namespace'),
    ]

    operations = [
        migrations.CreateModel(
            name='LinkedPullRequestMergeAttestation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(max_length=32)),
                ('repo', models.CharField(max_length=255)),
                ('number', models.PositiveIntegerField()),
                ('url', models.URLField(max_length=2048)),
                ('head_sha', models.CharField(max_length=64)),
                ('merge_commit', models.CharField(db_index=True, max_length=64)),
                ('merged_at', models.DateTimeField()),
                ('attested_by', models.CharField(default='pr-sync', max_length=120)),
                ('attested_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='merge_attestations', to='tasks.item')),
            ],
            options={'db_table': 'tasks_linked_pr_merge_attestation'},
        ),
        migrations.AddConstraint(
            model_name='linkedpullrequestmergeattestation',
            constraint=models.UniqueConstraint(fields=('item', 'repo', 'number'), name='tasks_prma_item_repo_number_uniq'),
        ),
        migrations.AddIndex(
            model_name='linkedpullrequestmergeattestation',
            index=models.Index(fields=['item', 'repo', 'number'], name='tasks_prma_lookup_idx'),
        ),
        migrations.AddField(
            model_name='verificationrequirementmanifest',
            name='merge_attestation',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='verification_requirement_manifests', to='tasks.linkedpullrequestmergeattestation'),
        ),
        migrations.AddField(
            model_name='verificationrequirementmanifest',
            name='pull_request_repo',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='verificationrequirementmanifest',
            name='pull_request_number',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(require_pre_activation_empty, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='verificationrequirementmanifest',
            name='merge_attestation',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='verification_requirement_manifests', to='tasks.linkedpullrequestmergeattestation'),
        ),
        migrations.AlterField(
            model_name='verificationrequirementmanifest',
            name='pull_request_repo',
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name='verificationrequirementmanifest',
            name='pull_request_number',
            field=models.PositiveIntegerField(),
        ),
    ]
