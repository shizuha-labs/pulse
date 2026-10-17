"""PLAT-10787 — re-key PR merge attestations by (item, repo, number, provider).

The mirror-era defect: the old (item, repo, number) uniqueness let a GitHub
mirror-era attestation row shadow the canonical Origin identity, freezing the
deployment-evidence gate on constant facts (specimens on PLAT-10787 / PLAT-9539
cmt 959644). Provider-scoped uniqueness lets one attested identity exist per
provider per linked PR; the freeze-gate lookup already filters merge_commit, so
coexisting provider rows are safe and a mirror-era row can never shadow a
canonical one.

Audit-preservation for the dedup below: any removed row is fully serialized to
the migration log via stdout before deletion, and the durable audit trail is the
linked item's comment history (runbook: post removed row contents to the owning
item before applying this migration in environments that predate the manual
PLAT-10787 purge).
"""
from django.db import migrations, models


def dedup_same_provider_rows(apps, schema_editor):
    """Keep the newest attestation per (item, repo, number, provider).

    Older duplicates (same provider, conflicting merge identity) are serialized
    to stdout for audit before removal; cross-provider rows are preserved.
    """
    LinkedPullRequestMergeAttestation = apps.get_model(
        'tasks', 'LinkedPullRequestMergeAttestation'
    )
    from collections import defaultdict
    groups = defaultdict(list)
    for row in LinkedPullRequestMergeAttestation.objects.all().order_by('attested_at', 'id'):
        groups[(row.item_id, row.repo, row.number, row.provider)].append(row)
    for key, rows in groups.items():
        for stale in rows[:-1]:
            print(
                f"[plat10787-audit] removing stale attestation "
                f"item_id={key[0]} repo={key[1]} number={key[2]} provider={key[3]} "
                f"id={stale.id} url={stale.url} head_sha={stale.head_sha} "
                f"merge_commit={stale.merge_commit} merged_at={stale.merged_at} "
                f"attested_by={stale.attested_by} attested_at={stale.attested_at}",
            )
            stale.delete()


def un_dedup(apps, schema_editor):
    # Irreversible by design: removed rows are audit-serialized above.
    pass


class Migration(migrations.Migration):

    dependencies = [
        # PLAT-10787: re-parent onto master's tip leaf — master advanced to
        # 0191_plat10330 after this migration was authored off 0190_merge,
        # leaving a two-leaf graph (specimen: premerge run 58120, leaf
        # conflict 0191/0200).
        ('tasks', '0191_plat10330_reviewer_pass_disposition'),
    ]

    operations = [
        migrations.RunPython(dedup_same_provider_rows, un_dedup),
        migrations.RemoveConstraint(
            model_name='linkedpullrequestmergeattestation',
            name='tasks_prma_item_repo_number_uniq',
        ),
        migrations.AddConstraint(
            model_name='linkedpullrequestmergeattestation',
            constraint=models.UniqueConstraint(
                fields=('item', 'repo', 'number', 'provider'),
                name='tasks_prma_item_repo_provider_uniq',
            ),
        ),
    ]
