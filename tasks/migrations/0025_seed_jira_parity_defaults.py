# Seed default data for Phase 5 Jira parity models
from django.db import migrations
import uuid


def seed_defaults(apps, schema_editor):
    IssueType = apps.get_model('tasks', 'IssueType')
    IssueTypeScheme = apps.get_model('tasks', 'IssueTypeScheme')
    IssueTypeSchemeMapping = apps.get_model('tasks', 'IssueTypeSchemeMapping')
    Screen = apps.get_model('tasks', 'Screen')
    ScreenScheme = apps.get_model('tasks', 'ScreenScheme')
    IssueTypeScreenScheme = apps.get_model('tasks', 'IssueTypeScreenScheme')
    PermissionScheme = apps.get_model('tasks', 'PermissionScheme')
    PermissionGrant = apps.get_model('tasks', 'PermissionGrant')
    NotificationScheme = apps.get_model('tasks', 'NotificationScheme')
    NotificationSchemeEvent = apps.get_model('tasks', 'NotificationSchemeEvent')
    BusinessCalendar = apps.get_model('tasks', 'BusinessCalendar')

    # -----------------------------------------------------------------------
    # 1. Issue Types
    # -----------------------------------------------------------------------
    issue_type_defs = [
        {
            'name': 'Epic',
            'slug': 'epic',
            'item_type_prefix': 'task.epic',
            'hierarchy_level': 0,
            'is_subtask': False,
            'icon': 'zap',
            'color': '#8b5cf6',
            'is_system': True,
            'position': 0,
        },
        {
            'name': 'Story',
            'slug': 'story',
            'item_type_prefix': 'task.story',
            'hierarchy_level': 1,
            'is_subtask': False,
            'icon': 'book-open',
            'color': '#22c55e',
            'is_system': True,
            'position': 1,
        },
        {
            'name': 'Bug',
            'slug': 'bug',
            'item_type_prefix': 'task.bug',
            'hierarchy_level': 1,
            'is_subtask': False,
            'icon': 'bug',
            'color': '#ef4444',
            'is_system': True,
            'position': 2,
        },
        {
            'name': 'Task',
            'slug': 'task',
            'item_type_prefix': 'task.generic',
            'hierarchy_level': 2,
            'is_subtask': False,
            'icon': 'check-square',
            'color': '#3b82f6',
            'is_system': True,
            'position': 3,
        },
        {
            'name': 'Sub-task',
            'slug': 'subtask',
            'item_type_prefix': 'task.subtask',
            'hierarchy_level': 3,
            'is_subtask': True,
            'icon': 'corner-down-right',
            'color': '#6b7280',
            'is_system': True,
            'position': 4,
        },
        {
            'name': 'Improvement',
            'slug': 'improvement',
            'item_type_prefix': 'task.improvement',
            'hierarchy_level': 1,
            'is_subtask': False,
            'icon': 'trending-up',
            'color': '#14b8a6',
            'is_system': False,
            'position': 5,
        },
        {
            'name': 'New Feature',
            'slug': 'new_feature',
            'item_type_prefix': 'task.feature',
            'hierarchy_level': 1,
            'is_subtask': False,
            'icon': 'plus-circle',
            'color': '#f59e0b',
            'is_system': False,
            'position': 6,
        },
    ]

    issue_types = {}  # slug → instance
    for it_def in issue_type_defs:
        slug = it_def['slug']
        obj, _ = IssueType.objects.get_or_create(
            slug=slug,
            defaults={
                'id': uuid.uuid4(),
                **{k: v for k, v in it_def.items() if k != 'slug'},
            },
        )
        issue_types[slug] = obj

    # -----------------------------------------------------------------------
    # 2. Issue Type Scheme (default) + mappings
    # -----------------------------------------------------------------------
    default_its, _ = IssueTypeScheme.objects.get_or_create(
        slug='default',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Issue Type Scheme',
            'is_system': True,
            'default_issue_type': issue_types['task'],
        },
    )
    # If it already existed, ensure default_issue_type is set
    if not default_its.default_issue_type_id:
        default_its.default_issue_type = issue_types['task']
        default_its.save()

    for it_def in issue_type_defs:
        IssueTypeSchemeMapping.objects.get_or_create(
            scheme=default_its,
            issue_type=issue_types[it_def['slug']],
            defaults={
                'id': uuid.uuid4(),
                'position': it_def['position'],
            },
        )

    # -----------------------------------------------------------------------
    # 3. Screens (create, edit, view)
    # -----------------------------------------------------------------------
    create_screen, _ = Screen.objects.get_or_create(
        slug='default-create',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Create Screen',
            'tabs': [{'name': 'Details', 'position': 0}],
            'is_system': True,
        },
    )

    edit_screen, _ = Screen.objects.get_or_create(
        slug='default-edit',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Edit Screen',
            'tabs': [{'name': 'Details', 'position': 0}],
            'is_system': True,
        },
    )

    view_screen, _ = Screen.objects.get_or_create(
        slug='default-view',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default View Screen',
            'tabs': [
                {'name': 'Details', 'position': 0},
                {'name': 'Dates', 'position': 1},
            ],
            'is_system': True,
        },
    )

    # -----------------------------------------------------------------------
    # 4. Screen Scheme (default)
    # -----------------------------------------------------------------------
    default_ss, _ = ScreenScheme.objects.get_or_create(
        slug='default',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Screen Scheme',
            'is_system': True,
            'create_screen': create_screen,
            'edit_screen': edit_screen,
            'view_screen': view_screen,
        },
    )

    # -----------------------------------------------------------------------
    # 5. Issue Type Screen Scheme (default)
    # -----------------------------------------------------------------------
    IssueTypeScreenScheme.objects.get_or_create(
        slug='default',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Issue Type Screen Scheme',
            'default_screen_scheme': default_ss,
            'is_system': True,
        },
    )

    # -----------------------------------------------------------------------
    # 6. Permission Scheme (default) + grants
    # -----------------------------------------------------------------------
    default_ps, _ = PermissionScheme.objects.get_or_create(
        slug='default',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Permission Scheme',
            'is_system': True,
        },
    )

    default_permissions = [
        'browse_projects',
        'create_issues',
        'edit_issues',
        'delete_issues',
        'close_issues',
        'transition_issues',
        'assign_issues',
        'assignable_user',
        'move_issues',
        'schedule_issues',
        'link_issues',
        'bulk_change',
        'add_comments',
        'edit_own_comments',
        'delete_own_comments',
        'create_attachments',
        'delete_own_attachments',
        'work_on_issues',
        'edit_own_worklogs',
        'delete_own_worklogs',
        'manage_watchers',
        'view_voters_and_watchers',
        'view_read_only_workflow',
    ]

    for perm in default_permissions:
        PermissionGrant.objects.get_or_create(
            scheme=default_ps,
            permission=perm,
            holder_type='logged_in',
            defaults={
                'id': uuid.uuid4(),
                'holder_value': '',
            },
        )

    # -----------------------------------------------------------------------
    # 7. Notification Scheme (default) + events
    # -----------------------------------------------------------------------
    default_ns, _ = NotificationScheme.objects.get_or_create(
        slug='default',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Default Notification Scheme',
            'is_system': True,
        },
    )

    notification_events = [
        # issue_created → reporter + watchers
        {'event_type': 'issue_created', 'recipient_type': 'reporter', 'recipient_value': ''},
        {'event_type': 'issue_created', 'recipient_type': 'watchers', 'recipient_value': ''},
        # issue_assigned → assignee
        {'event_type': 'issue_assigned', 'recipient_type': 'assignee', 'recipient_value': ''},
        # issue_resolved → reporter + assignee + watchers
        {'event_type': 'issue_resolved', 'recipient_type': 'reporter', 'recipient_value': ''},
        {'event_type': 'issue_resolved', 'recipient_type': 'assignee', 'recipient_value': ''},
        {'event_type': 'issue_resolved', 'recipient_type': 'watchers', 'recipient_value': ''},
        # issue_closed → reporter + watchers
        {'event_type': 'issue_closed', 'recipient_type': 'reporter', 'recipient_value': ''},
        {'event_type': 'issue_closed', 'recipient_type': 'watchers', 'recipient_value': ''},
        # comment_created → assignee + reporter + watchers
        {'event_type': 'comment_created', 'recipient_type': 'assignee', 'recipient_value': ''},
        {'event_type': 'comment_created', 'recipient_type': 'reporter', 'recipient_value': ''},
        {'event_type': 'comment_created', 'recipient_type': 'watchers', 'recipient_value': ''},
    ]

    for evt in notification_events:
        NotificationSchemeEvent.objects.get_or_create(
            scheme=default_ns,
            event_type=evt['event_type'],
            recipient_type=evt['recipient_type'],
            defaults={
                'id': uuid.uuid4(),
                'recipient_value': evt['recipient_value'],
            },
        )

    # -----------------------------------------------------------------------
    # 8. Business Calendar (standard)
    # -----------------------------------------------------------------------
    BusinessCalendar.objects.get_or_create(
        slug='standard',
        defaults={
            'id': uuid.uuid4(),
            'name': 'Standard Business Hours',
            'timezone': 'UTC',
            'working_hours': {
                'monday': {'start': '09:00', 'end': '17:00'},
                'tuesday': {'start': '09:00', 'end': '17:00'},
                'wednesday': {'start': '09:00', 'end': '17:00'},
                'thursday': {'start': '09:00', 'end': '17:00'},
                'friday': {'start': '09:00', 'end': '17:00'},
            },
            'holidays': [],
        },
    )


def reverse_seed(apps, schema_editor):
    IssueType = apps.get_model('tasks', 'IssueType')
    IssueTypeScheme = apps.get_model('tasks', 'IssueTypeScheme')
    Screen = apps.get_model('tasks', 'Screen')
    ScreenScheme = apps.get_model('tasks', 'ScreenScheme')
    IssueTypeScreenScheme = apps.get_model('tasks', 'IssueTypeScreenScheme')
    PermissionScheme = apps.get_model('tasks', 'PermissionScheme')
    NotificationScheme = apps.get_model('tasks', 'NotificationScheme')
    BusinessCalendar = apps.get_model('tasks', 'BusinessCalendar')

    # Delete in reverse dependency order
    BusinessCalendar.objects.filter(slug='standard').delete()
    NotificationScheme.objects.filter(slug='default').delete()
    PermissionScheme.objects.filter(slug='default').delete()
    IssueTypeScreenScheme.objects.filter(slug='default').delete()
    ScreenScheme.objects.filter(slug='default').delete()
    Screen.objects.filter(slug__in=['default-create', 'default-edit', 'default-view']).delete()
    IssueTypeScheme.objects.filter(slug='default').delete()
    IssueType.objects.filter(
        slug__in=['epic', 'story', 'bug', 'task', 'subtask', 'improvement', 'new_feature']
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0024_add_jira_parity_models'),
    ]

    operations = [
        migrations.RunPython(seed_defaults, reverse_seed),
    ]
