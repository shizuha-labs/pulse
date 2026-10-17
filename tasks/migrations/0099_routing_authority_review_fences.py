import uuid

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('tasks', '0098_routing_authority_bindings')]

    operations = [
        migrations.AddField(
            model_name='routingauthoritycheckpoint',
            name='first_received_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='routingauthoritycheckpoint',
            name='source_event_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='routingauthoritycheckpoint',
            name='source_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='CrossOrgAssignmentDelegation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('source_organization_id', models.PositiveIntegerField(db_index=True)),
                ('target_organization_id', models.PositiveIntegerField(db_index=True)),
                ('action', models.CharField(default='receive_assignment', max_length=64)),
                ('state', models.CharField(choices=[('active', 'Active'),
                                                    ('revoked', 'Revoked')], max_length=20)),
                ('version', models.PositiveBigIntegerField()),
                ('is_current', models.BooleanField(db_index=True, default=True)),
                ('subject_generation', models.PositiveBigIntegerField()),
                ('revocation_generation', models.PositiveBigIntegerField(default=0)),
                ('expires_at', models.DateTimeField()),
                ('command_id', models.UUIDField()),
                ('origin_event_id', models.UUIDField(default=uuid.uuid4, unique=True)),
                ('digest', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='cross_org_assignment_delegations', to='tasks.project')),
                ('routing_team', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='cross_org_delegation_versions', to='tasks.routingteam')),
                ('subject', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='cross_org_delegation_versions', to='tasks.routingsubject')),
            ],
            options={'db_table': 'tasks_cross_org_assignment_delegation'},
        ),
        migrations.AddConstraint(
            model_name='crossorgassignmentdelegation',
            constraint=models.UniqueConstraint(
                fields=('source_organization_id', 'target_organization_id', 'subject',
                        'routing_team', 'project', 'action', 'version'),
                name='uniq_cross_org_delegation_version'),
        ),
        migrations.AddConstraint(
            model_name='crossorgassignmentdelegation',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_current', True)),
                fields=('source_organization_id', 'target_organization_id', 'subject',
                        'routing_team', 'project', 'action'),
                name='uniq_cross_org_delegation_current'),
        ),
        migrations.CreateModel(
            name='RoutingAssignmentReceipt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('action', models.CharField(choices=[
                    ('assign', 'Assign concrete owner'),
                    ('clear', 'Clear owner to group queue'),
                    ('unassign', 'Explicitly unassign and clear routing group'),
                    ('reroute_group_queue', 'Reroute to exact group queue'),
                ], default='assign', max_length=32)),
                ('reason', models.CharField(blank=True, default='', max_length=100)),
                ('organization_id', models.PositiveIntegerField()),
                ('assignment_group', models.CharField(max_length=100)),
                ('workflow_name', models.CharField(max_length=100)),
                ('subject_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('assignee_email', models.EmailField(
                    blank=True, default='', max_length=254)),
                ('binding_version', models.PositiveBigIntegerField()),
                ('entitlement_version', models.PositiveBigIntegerField()),
                ('delegation_version', models.PositiveBigIntegerField(blank=True, null=True)),
                ('subject_generation', models.PositiveBigIntegerField()),
                ('revocation_high_water', models.PositiveBigIntegerField()),
                ('freshness_vector_digest', models.CharField(max_length=64)),
                ('result_organization_id', models.PositiveIntegerField()),
                ('result_assignment_group', models.CharField(
                    blank=True, default='', max_length=100)),
                ('result_workflow_name', models.CharField(
                    blank=True, default='', max_length=100)),
                ('expires_at', models.DateTimeField()),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='routing_assignment_receipts', to='tasks.item')),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='routing_assignment_receipts', to='tasks.project')),
                ('result_project', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='routing_assignment_result_receipts', to='tasks.project')),
                ('result_routing_team', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name='assignment_result_receipts', to='tasks.routingteam')),
                ('routing_team', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name='assignment_receipts', to='tasks.routingteam')),
            ],
            options={'db_table': 'tasks_routing_assignment_receipt'},
        ),
        migrations.AddIndex(
            model_name='routingassignmentreceipt',
            index=models.Index(fields=['item', 'consumed_at', 'expires_at'],
                               name='routing_receipt_consume_idx'),
        ),
        migrations.CreateModel(
            name='RoutingAuthorityQuarantine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('consumer', models.CharField(max_length=100)),
                ('authority_kind', models.CharField(max_length=64)),
                ('aggregate_key', models.CharField(max_length=255)),
                ('raw_event', models.JSONField(default=dict)),
                ('reason', models.CharField(max_length=100)),
                ('dedupe_key', models.CharField(max_length=64, unique=True)),
                ('recovered_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'db_table': 'tasks_routing_authority_quarantine'},
        ),
        migrations.CreateModel(
            name='RoutingAuthorityHoldSignal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('authority_id', models.CharField(max_length=64)),
                ('aggregate_key', models.CharField(max_length=255)),
                ('checkpoint_generation', models.PositiveBigIntegerField(default=0)),
                ('reason', models.CharField(max_length=100)),
                ('owner_group', models.CharField(default='devops', max_length=64)),
                ('dedupe_key', models.CharField(max_length=64, unique=True)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField()),
                ('in_flight', models.BooleanField(db_index=True, default=False)),
                ('in_flight_until', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
                ('notification_recipient', models.EmailField(blank=True, default='',
                                                              max_length=254)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'tasks_routing_authority_hold_signal'},
        ),
        migrations.AddIndex(
            model_name='routingauthorityholdsignal',
            index=models.Index(fields=['resolved_at', 'in_flight', 'next_attempt_at'],
                               name='routing_hold_ready_idx'),
        ),
        migrations.AddField(
            model_name='routingauthorityoutbox',
            name='in_flight',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='routingauthorityoutbox',
            name='in_flight_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='routingauthorityoutbox',
            name='next_attempt_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.RunSQL(
            sql=r'''
CREATE OR REPLACE FUNCTION tasks_guard_item_assignment()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_project_org bigint;
    new_project_org bigint;
    old_team uuid;
    new_team uuid;
    assignment_changed boolean;
    routing_context_changed boolean;
    old_assigned boolean;
    clear_write boolean;
    clear_action text;
    receipt_id uuid;
    matched_receipt uuid;
BEGIN
    IF TG_OP = 'INSERT'
       AND NEW.assignee_id IS NULL
       AND coalesce(NEW.assignee_email, '') = '' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        assignment_changed := (
            OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
            OR lower(coalesce(OLD.assignee_email, ''))
               <> lower(coalesce(NEW.assignee_email, ''))
        );
        routing_context_changed := (
            OLD.project_id IS DISTINCT FROM NEW.project_id
            OR replace(lower(coalesce(OLD.assignment_group, '')), '_', '-')
               <> replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
            OR coalesce(OLD.workflow_name, '') <> coalesce(NEW.workflow_name, '')
        );
        old_assigned := (
            OLD.assignee_id IS NOT NULL OR coalesce(OLD.assignee_email, '') <> ''
        );
        IF NOT assignment_changed
           AND NOT (old_assigned AND routing_context_changed) THEN
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.project_id IS NOT NULL THEN
        SELECT org_id INTO old_project_org FROM tasks_project WHERE id = OLD.project_id;
        IF coalesce(OLD.assignment_group, '') <> '' THEN
            SELECT id INTO old_team
              FROM tasks_routing_team
             WHERE organization_id = old_project_org
               AND key = replace(lower(OLD.assignment_group), '_', '-');
        END IF;
    END IF;
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_project_org FROM tasks_project WHERE id = NEW.project_id;
        IF coalesce(NEW.assignment_group, '') <> '' THEN
            SELECT id INTO new_team
              FROM tasks_routing_team
             WHERE organization_id = new_project_org
               AND key = replace(lower(NEW.assignment_group), '_', '-');
        END IF;
    END IF;

    IF old_team IS NULL AND new_team IS NULL THEN
        RETURN NEW;
    END IF;

    clear_write := FALSE;
    IF TG_OP = 'UPDATE' THEN
        clear_write := (
            assignment_changed
            AND (OLD.assignee_id IS NOT NULL
                 OR coalesce(OLD.assignee_email, '') <> '')
            AND NEW.assignee_id IS NULL
            AND coalesce(NEW.assignee_email, '') = ''
        );
        IF clear_write THEN
            IF NOT routing_context_changed THEN
                clear_action := 'clear';
            ELSIF OLD.project_id IS NOT DISTINCT FROM NEW.project_id
                  AND coalesce(OLD.workflow_name, '') = coalesce(NEW.workflow_name, '')
                  AND coalesce(NEW.assignment_group, '') = '' THEN
                clear_action := 'unassign';
            ELSE
                clear_action := 'reroute_group_queue';
            END IF;
        END IF;
    END IF;

    BEGIN
        receipt_id := nullif(current_setting('pulse.routing_assignment_receipt', true), '')::uuid;
    EXCEPTION WHEN OTHERS THEN
        receipt_id := NULL;
    END;
    IF clear_write THEN
        SELECT id INTO matched_receipt
         FROM tasks_routing_assignment_receipt
         WHERE id = receipt_id
           AND action = clear_action
           AND item_id = OLD.id
           AND organization_id = old_project_org
           AND project_id = OLD.project_id
           AND routing_team_id IS NOT DISTINCT FROM old_team
           AND assignment_group = replace(lower(OLD.assignment_group), '_', '-')
           AND workflow_name = coalesce(OLD.workflow_name, '')
           AND subject_id IS NOT DISTINCT FROM OLD.assignee_id
           AND lower(assignee_email) = lower(coalesce(OLD.assignee_email, ''))
           AND result_organization_id = new_project_org
           AND result_project_id = NEW.project_id
           AND result_routing_team_id IS NOT DISTINCT FROM new_team
           AND result_assignment_group = replace(lower(coalesce(NEW.assignment_group, '')), '_', '-')
           AND result_workflow_name = coalesce(NEW.workflow_name, '')
           AND consumed_at IS NULL
           AND expires_at >= clock_timestamp()
         FOR UPDATE;
    ELSE
        IF new_team IS NULL
           OR NEW.assignee_id IS NULL
           OR coalesce(NEW.assignee_email, '') = '' THEN
            RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
              USING ERRCODE = '42501';
        END IF;
        SELECT id INTO matched_receipt
          FROM tasks_routing_assignment_receipt
         WHERE id = receipt_id
           AND action = 'assign'
           AND item_id = NEW.id
           AND organization_id = new_project_org
           AND project_id = NEW.project_id
           AND routing_team_id = new_team
           AND assignment_group = replace(lower(NEW.assignment_group), '_', '-')
           AND workflow_name = coalesce(NEW.workflow_name, '')
           AND subject_id = NEW.assignee_id
           AND lower(assignee_email) = lower(coalesce(NEW.assignee_email, ''))
           AND result_organization_id = new_project_org
           AND result_project_id = NEW.project_id
           AND result_routing_team_id = new_team
           AND result_assignment_group = replace(lower(NEW.assignment_group), '_', '-')
           AND result_workflow_name = coalesce(NEW.workflow_name, '')
           AND consumed_at IS NULL
           AND expires_at >= clock_timestamp()
         FOR UPDATE;
    END IF;
    IF matched_receipt IS NULL THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_required'
          USING ERRCODE = '42501';
    END IF;
    UPDATE tasks_routing_assignment_receipt
       SET consumed_at = clock_timestamp()
     WHERE id = matched_receipt AND consumed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'routing_authority:assignment_receipt_reused'
          USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_item_assignment_guard
BEFORE INSERT OR UPDATE OF assignee_id, assignee_email, project_id, assignment_group, workflow_name
ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_assignment();
''',
            reverse_sql=r'''
DROP TRIGGER IF EXISTS tasks_item_assignment_guard ON tasks_task;
DROP FUNCTION IF EXISTS tasks_guard_item_assignment();
''',
        ),
    ]
