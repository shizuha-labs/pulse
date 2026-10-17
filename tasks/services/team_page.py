"""Aggregates for the Team Tasks page.

The page used to materialize every open task and call Identity once per
assignee. Counts come from GROUP BY. Task bodies load only for the row
the caller expands.
"""
from django.db.models import Count, Max, Q, Value
from django.db.models.functions import Coalesce, Lower, NullIf

MEMBER_TASK_LIMIT = 40


def assignee_summary(queryset):
    rows = (
        queryset.exclude(assignee_email__isnull=True)
        .exclude(assignee_email='')
        .annotate(email_key=Lower('assignee_email'))
        .values('email_key')
        .annotate(
            task_count=Count('id'),
            assignee_id=Max('assignee_id'),
            assignee_email=Max('assignee_email'),
        )
        .order_by('-task_count', 'email_key')
    )
    result = []
    for row in rows:
        email = row['assignee_email'] or row['email_key']
        result.append({
            'assignee_id': row['assignee_id'],
            'assignee_email': email,
            'assignee_name': email.split('@', 1)[0],
            'task_count': row['task_count'],
            'tasks': [],
        })
    return result


def team_summary(queryset):
    rows = (
        queryset.annotate(
            team_key=Coalesce(
                NullIf('assignment_group', Value('')),
                Value('Unassigned'),
            )
        )
        .values('team_key')
        .annotate(task_count=Count('id'))
        .order_by('-task_count', 'team_key')
    )
    unassigned = []
    named = []
    for row in rows:
        entry = {
            'team_name': row['team_key'],
            'task_count': row['task_count'],
            'tasks': [],
        }
        if row['team_key'] == 'Unassigned':
            unassigned.append(entry)
        else:
            named.append(entry)
    return named + unassigned


def workload_summary(queryset, now):
    assigned_rows = (
        queryset.exclude(assignee_id__isnull=True)
        .values('assignee_id')
        .annotate(
            email=Max('assignee_email'),
            total=Count('id'),
            pending=Count('id', filter=Q(status='pending')),
            in_progress=Count('id', filter=Q(status='in_progress')),
            completed=Count('id', filter=Q(status='completed')),
            overdue=Count(
                'id',
                filter=Q(due_date__lt=now, status__in=['pending', 'in_progress']),
            ),
        )
    )
    created_rows = {
        row['created_by_id']: row
        for row in (
            queryset.exclude(created_by_id__isnull=True)
            .values('created_by_id')
            .annotate(created=Count('id'), email=Max('created_by_email'))
        )
    }
    by_id = {}
    for row in assigned_rows:
        by_id[row['assignee_id']] = {
            'user_id': row['assignee_id'],
            'email': row['email'] or f"user_{row['assignee_id']}@shizuha.id",
            'assigned': {
                'total': row['total'],
                'pending': row['pending'],
                'in_progress': row['in_progress'],
                'completed': row['completed'],
                'overdue': row['overdue'],
            },
            'created': 0,
        }
    for uid, row in created_rows.items():
        entry = by_id.get(uid)
        if entry is None:
            entry = {
                'user_id': uid,
                'email': row['email'] or f'user_{uid}@shizuha.id',
                'assigned': {
                    'total': 0,
                    'pending': 0,
                    'in_progress': 0,
                    'completed': 0,
                    'overdue': 0,
                },
                'created': 0,
            }
            by_id[uid] = entry
        entry['created'] = row['created']
        if entry['email'].endswith('@shizuha.id') and row['email']:
            entry['email'] = row['email']
    result = []
    for entry in by_id.values():
        total = entry['assigned']['total']
        created = entry['created']
        if total <= 0 and created <= 0:
            continue
        completed = entry['assigned']['completed']
        entry['completion_rate'] = round(completed / total * 100, 1) if total else 0
        result.append(entry)
    result.sort(key=lambda row: row['assigned']['total'], reverse=True)
    return result


def annotate_list_comments(queryset):
    """One comment aggregate per row, matching ItemListSerializer's fallback."""
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Coalesce as _Coalesce

    from tasks.models import Comment

    comments = Comment.objects.filter(
        item=OuterRef('pk'),
        origin_kind__in=Comment.SUBSTANTIVE_ORIGIN_KINDS,
    )
    return queryset.annotate(
        _comment_count=_Coalesce(
            Subquery(comments.values('item').annotate(c=Count('id')).values('c')),
            0,
        ),
        _last_comment_at=Subquery(
            comments.order_by('-created_at').values('created_at')[:1]
        ),
    )
