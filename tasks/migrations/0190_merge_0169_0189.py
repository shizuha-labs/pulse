from django.db import migrations


class Migration(migrations.Migration):
    """Merge the parallel migration leaves left by PLAT-4182 and PLAT-10083.

    Two recently-merged branches each appended a leaf to the ``tasks``
    migration graph without converging on a common child:

    * ``0169_plat4182_qnt_artifact_retention_gate`` (QNT artifact retention
      gate data migration), and
    * ``0189_plat10083_provider_wait_marker`` (ProviderWaitMarker model).

    Django refuses to order multiple leaf nodes, so ``migrate`` applied
    nothing on a fresh test database and the backend suite died on
    ``relation "django_celery_beat_crontabschedule" does not exist``
    (premerge red at 9eb064ca). This no-op merge node declares both leaves
    as parents, restoring a single leaf so the graph orders deterministically.
    Generated per ``manage.py makemigrations --merge`` semantics.
    """

    dependencies = [
        ('tasks', '0169_plat4182_qnt_artifact_retention_gate'),
        ('tasks', '0189_plat10083_provider_wait_marker'),
    ]

    operations = []
