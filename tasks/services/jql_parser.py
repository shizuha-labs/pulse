"""
JQL (Query Language) Parser for Shizuha Pulse.

Parses JQL-like query strings and converts them to Django Q objects.

Supported syntax:
- Comparisons: =, !=, ~, !~, >, <, >=, <=, IN, NOT IN, IS, IS NOT
- Logical: AND, OR, NOT, parentheses
- Functions: currentUser(), now(), startOfDay(), startOfWeek(), startOfMonth(), endOfDay(), endOfWeek(), endOfMonth()
- Fields: status, priority, assignee, component, fixVersion, affectsVersions, sprint, labels, due, created, updated, project, etc.

Examples:
    status = "pending" AND priority = "high"
    assignee = currentUser() AND due < now()
    project = "PROJ" AND (status = "pending" OR status = "in_progress")
    title ~ "bug"
    fixVersion = "2.0"
    component IS NOT EMPTY
"""

import re
from datetime import datetime, timedelta
from django.db.models import Q
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


class JQLSyntaxError(Exception):
    """Exception raised for JQL syntax errors."""
    pass


class JQLParser:
    """
    Parses JQL query strings and converts them to Django Q objects.
    """

    # Field mappings from JQL names to model fields
    FIELD_MAPPING = {
        # Basic fields
        'status': 'status',
        'priority': 'priority',
        'severity': 'severity',
        'mode': 'mode',
        'type': 'item_type',
        'itemtype': 'item_type',
        'item_type': 'item_type',

        # Assignment
        'assignee': 'assignee_email',
        'assignee_id': 'assignee_id',
        'reporter': 'created_by_email',
        'creator': 'created_by_email',
        'created_by': 'created_by_email',

        # Project & organization
        'project': 'project__key',
        'project_id': 'project_id',

        # Sprint
        'sprint': 'sprint__name',
        'sprint_id': 'sprint_id',

        # Component & Version (Phase 3)
        'component': 'component__name',
        'component_id': 'component_id',
        'fixversion': 'fix_version__name',
        'fix_version': 'fix_version__name',
        'fixversion_id': 'fix_version_id',
        'affectsversion': 'affects_versions__name',
        'affects_version': 'affects_versions__name',

        # Text fields
        'title': 'title',
        'summary': 'title',
        'description': 'description',
        'key': 'item_key',
        'item_key': 'item_key',

        # Dates
        'due': 'due_date',
        'duedate': 'due_date',
        'due_date': 'due_date',
        'created': 'created_at',
        'createdat': 'created_at',
        'created_at': 'created_at',
        'updated': 'updated_at',
        'updatedat': 'updated_at',
        'updated_at': 'updated_at',
        'completed': 'completed_at',
        'completedat': 'completed_at',

        # Time tracking (Phase 3)
        'timespent': 'time_spent_minutes',
        'time_spent': 'time_spent_minutes',
        'originalestimate': 'original_estimate_minutes',
        'original_estimate': 'original_estimate_minutes',
        'remainingestimate': 'remaining_estimate_minutes',
        'remaining_estimate': 'remaining_estimate_minutes',

        # Story points
        'storypoints': 'story_points',
        'story_points': 'story_points',
        'points': 'story_points',

        # Labels and tags
        'labels': 'labels',
        'tags': 'tags',

        # Source
        'source': 'source',

        # Workflow (Phase 4)
        'workflow': 'workflow_name',
        'workflow_name': 'workflow_name',
        'workflowstatus': 'workflow_status',
        'workflow_status': 'workflow_status',
    }

    # Operators
    OPERATORS = ['!=', '>=', '<=', '!~', '=', '~', '>', '<']
    KEYWORDS = ['AND', 'OR', 'NOT', 'IN', 'IS', 'EMPTY', 'NULL', 'ORDER', 'BY', 'ASC', 'DESC']

    def __init__(self):
        self.user = None
        self.tokens = []
        self.pos = 0

    def parse(self, jql_string, user=None):
        """
        Parse a JQL string and return a Django Q object.

        Args:
            jql_string: The JQL query string
            user: The current user (for currentUser() function)

        Returns:
            Django Q object
        """
        self.user = user
        self.tokens = self._tokenize(jql_string)
        self.pos = 0

        if not self.tokens:
            return Q()

        try:
            result = self._parse_expression()

            # Check for ORDER BY (we'll ignore it for now, sorting is handled separately)
            if self._peek() and self._peek().upper() == 'ORDER':
                self._consume('ORDER')
                self._consume('BY')
                # Skip order by clause
                while self._peek():
                    self._advance()

            # PLS-831: reject leftover tokens. A malformed query like
            # `status === 'in_progress'` tokenizes to `status = = = in_progress`;
            # after parsing `status = '='` the trailing `= in_progress` must be a
            # syntax error, never silently dropped (which degraded to an
            # unfiltered/empty query).
            if self._peek():
                remaining = ' '.join(str(t) for t in self.tokens[self.pos:])
                raise JQLSyntaxError(
                    f"Unexpected token(s) after query: '{remaining}'. "
                    f"Check operators (use single '=', not '===') and complete "
                    f"the expression."
                )

            return result
        except JQLSyntaxError:
            # Already a clean parse error — bubble up without re-logging at ERROR.
            # User input errors are not alert-worthy.
            raise
        except Exception as e:
            # Unexpected parser bug — log at WARNING (no alert spam) and wrap
            logger.warning(f"JQL parse error: {e} for query: {jql_string}")
            raise JQLSyntaxError(f"Invalid JQL syntax: {str(e)}")

    def _tokenize(self, jql_string):
        """Tokenize the JQL string."""
        tokens = []
        i = 0
        jql_string = jql_string.strip()

        while i < len(jql_string):
            # Skip whitespace
            if jql_string[i].isspace():
                i += 1
                continue

            # Parentheses
            if jql_string[i] in '()':
                tokens.append(jql_string[i])
                i += 1
                continue

            # Comma (for IN lists)
            if jql_string[i] == ',':
                tokens.append(',')
                i += 1
                continue

            # Quoted string
            if jql_string[i] in '"\'':
                quote = jql_string[i]
                i += 1
                start = i
                while i < len(jql_string) and jql_string[i] != quote:
                    if jql_string[i] == '\\' and i + 1 < len(jql_string):
                        i += 2
                    else:
                        i += 1
                tokens.append(jql_string[start:i])
                i += 1  # Skip closing quote
                continue

            # Two-character operators
            if i + 1 < len(jql_string) and jql_string[i:i+2] in self.OPERATORS:
                tokens.append(jql_string[i:i+2])
                i += 2
                continue

            # Single-character operators
            if jql_string[i] in '=~><':
                tokens.append(jql_string[i])
                i += 1
                continue

            # Word (field name, keyword, or value)
            start = i
            while i < len(jql_string) and not jql_string[i].isspace() and jql_string[i] not in '(),"\'=~><,':
                i += 1
            word = jql_string[start:i]
            if word:
                tokens.append(word)

        return tokens

    def _peek(self, offset=0):
        """Look at a token without consuming it."""
        if self.pos + offset < len(self.tokens):
            return self.tokens[self.pos + offset]
        return None

    def _advance(self):
        """Consume and return the current token."""
        if self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            self.pos += 1
            return token
        return None

    def _consume(self, expected=None):
        """Consume a token, optionally checking it matches expected."""
        token = self._advance()
        if expected and token and token.upper() != expected.upper():
            raise JQLSyntaxError(f"Expected '{expected}', got '{token}'")
        return token

    def _parse_expression(self):
        """Parse a full expression with OR precedence."""
        left = self._parse_and()

        while self._peek() and self._peek().upper() == 'OR':
            self._consume('OR')
            right = self._parse_and()
            left = left | right

        return left

    def _parse_and(self):
        """Parse AND expressions."""
        left = self._parse_not()

        while self._peek() and self._peek().upper() == 'AND':
            self._consume('AND')
            right = self._parse_not()
            left = left & right

        return left

    def _parse_not(self):
        """Parse NOT expressions."""
        if self._peek() and self._peek().upper() == 'NOT':
            self._consume('NOT')
            return ~self._parse_not()
        return self._parse_primary()

    def _parse_primary(self):
        """Parse primary expressions (comparisons and parenthesized expressions)."""
        token = self._peek()

        if token == '(':
            self._consume('(')
            result = self._parse_expression()
            self._consume(')')
            return result

        return self._parse_comparison()

    def _parse_comparison(self):
        """Parse a comparison expression (field op value)."""
        field = self._advance()
        if not field:
            raise JQLSyntaxError("Expected field name")

        # Check for IS [NOT] EMPTY/NULL
        next_token = self._peek()
        if next_token and next_token.upper() == 'IS':
            self._consume('IS')
            negate = False
            if self._peek() and self._peek().upper() == 'NOT':
                self._consume('NOT')
                negate = True

            empty_type = self._advance()
            if empty_type.upper() not in ('EMPTY', 'NULL'):
                raise JQLSyntaxError(f"Expected EMPTY or NULL, got '{empty_type}'")

            return self._build_is_empty_q(field, negate)

        # Check for [NOT] IN
        if next_token and next_token.upper() == 'NOT':
            self._consume('NOT')
            self._consume('IN')
            values = self._parse_in_list()
            return self._build_not_in_q(field, values)

        if next_token and next_token.upper() == 'IN':
            self._consume('IN')
            values = self._parse_in_list()
            return self._build_in_q(field, values)

        # Regular comparison
        operator = self._advance()
        if operator not in self.OPERATORS:
            raise JQLSyntaxError(f"Expected operator, got '{operator}'")

        value = self._parse_value()

        return self._build_comparison_q(field, operator, value)

    def _parse_in_list(self):
        """Parse an IN list like (val1, val2, val3)."""
        self._consume('(')
        values = []
        while True:
            value = self._parse_value()
            values.append(value)
            if self._peek() == ',':
                self._consume(',')
            elif self._peek() == ')':
                break
            else:
                break
        self._consume(')')
        return values

    def _parse_value(self):
        """Parse a value (string, number, or function call)."""
        token = self._advance()
        if not token:
            raise JQLSyntaxError("Expected value")

        # Check for function call
        if self._peek() == '(':
            return self._parse_function(token)

        # Return raw value
        return token

    def _parse_function(self, func_name):
        """Parse and evaluate a function call."""
        self._consume('(')

        # Get arguments
        args = []
        while self._peek() and self._peek() != ')':
            args.append(self._advance())
            if self._peek() == ',':
                self._consume(',')

        self._consume(')')

        return self._evaluate_function(func_name, args)

    def _evaluate_function(self, func_name, args):
        """Evaluate a JQL function."""
        func_name = func_name.lower()
        now = timezone.now()

        if func_name == 'currentuser':
            if self.user:
                return self.user.email
            return None

        elif func_name == 'now':
            return now

        elif func_name == 'startofday':
            days_offset = int(args[0]) if args else 0
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start + timedelta(days=days_offset)

        elif func_name == 'endofday':
            days_offset = int(args[0]) if args else 0
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            return end + timedelta(days=days_offset)

        elif func_name == 'startofweek':
            weeks_offset = int(args[0]) if args else 0
            days_to_monday = now.weekday()
            start = (now - timedelta(days=days_to_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start + timedelta(weeks=weeks_offset)

        elif func_name == 'endofweek':
            weeks_offset = int(args[0]) if args else 0
            days_to_sunday = 6 - now.weekday()
            end = (now + timedelta(days=days_to_sunday)).replace(hour=23, minute=59, second=59, microsecond=999999)
            return end + timedelta(weeks=weeks_offset)

        elif func_name == 'startofmonth':
            months_offset = int(args[0]) if args else 0
            year = now.year
            month = now.month + months_offset
            while month <= 0:
                year -= 1
                month += 12
            while month > 12:
                year += 1
                month -= 12
            return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)

        elif func_name == 'endofmonth':
            months_offset = int(args[0]) if args else 0
            # Start of next month minus 1 microsecond
            year = now.year
            month = now.month + months_offset + 1
            while month <= 0:
                year -= 1
                month += 12
            while month > 12:
                year += 1
                month -= 12
            start_of_next = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
            return start_of_next - timedelta(microseconds=1)

        else:
            raise JQLSyntaxError(f"Unknown function: {func_name}")

    # Safe fields that can be passed through directly (known Item model fields)
    _SAFE_RAW_FIELDS = {
        'workflow_name', 'workflow_status', 'item_key', 'item_type',
        'priority', 'severity', 'status', 'mode', 'source',
        'assignee_id', 'assignee_email', 'created_by_id', 'created_by_email',
        'project_id', 'sprint_id', 'component_id', 'fix_version_id',
        'story_points', 'due_date', 'created_at', 'updated_at', 'completed_at',
    }

    def _get_model_field(self, jql_field):
        """Convert JQL field name to model field name."""
        jql_field_lower = jql_field.lower()
        if jql_field_lower in self.FIELD_MAPPING:
            return self.FIELD_MAPPING[jql_field_lower]
        if jql_field_lower in self._SAFE_RAW_FIELDS:
            return jql_field_lower
        # Unknown field — raise a clean error instead of causing a Django FieldError
        raise JQLSyntaxError(
            f"Unknown field '{jql_field}'. "
            f"Known fields: status, priority, workflow, assignee, reporter, project, sprint, ..."
        )

    def _build_comparison_q(self, field, operator, value):
        """Build a Q object for a comparison."""
        model_field = self._get_model_field(field)

        # Handle special value conversions
        value = self._convert_value(value, model_field)

        if operator == '=':
            return Q(**{model_field: value})

        elif operator == '!=':
            return ~Q(**{model_field: value})

        elif operator == '>':
            return Q(**{f'{model_field}__gt': value})

        elif operator == '<':
            return Q(**{f'{model_field}__lt': value})

        elif operator == '>=':
            return Q(**{f'{model_field}__gte': value})

        elif operator == '<=':
            return Q(**{f'{model_field}__lte': value})

        elif operator == '~':
            # Contains (case insensitive)
            return Q(**{f'{model_field}__icontains': value})

        elif operator == '!~':
            # Not contains
            return ~Q(**{f'{model_field}__icontains': value})

        else:
            raise JQLSyntaxError(f"Unknown operator: {operator}")

    def _build_in_q(self, field, values):
        """Build a Q object for IN clause."""
        model_field = self._get_model_field(field)
        converted_values = [self._convert_value(v, model_field) for v in values]
        return Q(**{f'{model_field}__in': converted_values})

    def _build_not_in_q(self, field, values):
        """Build a Q object for NOT IN clause."""
        model_field = self._get_model_field(field)
        converted_values = [self._convert_value(v, model_field) for v in values]
        return ~Q(**{f'{model_field}__in': converted_values})

    def _build_is_empty_q(self, field, negate=False):
        """Build a Q object for IS [NOT] EMPTY/NULL."""
        model_field = self._get_model_field(field)

        # For string fields, empty means null or empty string
        if model_field in ['title', 'description', 'tags', 'assignee_email', 'created_by_email']:
            q = Q(**{f'{model_field}__isnull': True}) | Q(**{model_field: ''})
        else:
            q = Q(**{f'{model_field}__isnull': True})

        return ~q if negate else q

    def _convert_value(self, value, field):
        """Convert a string value to the appropriate type."""
        if value is None:
            return None

        # Already converted (e.g., from function)
        if isinstance(value, (datetime, int, float)):
            return value

        # Numeric fields
        numeric_fields = [
            'assignee_id', 'created_by_id', 'project_id', 'sprint_id',
            'component_id', 'fix_version_id', 'story_points',
            'time_spent_minutes', 'original_estimate_minutes', 'remaining_estimate_minutes'
        ]
        if field in numeric_fields:
            try:
                return int(value)
            except ValueError:
                return value

        # Date fields - try to parse
        date_fields = ['due_date', 'created_at', 'updated_at', 'completed_at']
        if field in date_fields and isinstance(value, str):
            try:
                # Try ISO format first
                if 'T' in value:
                    return datetime.fromisoformat(value.replace('Z', '+00:00'))
                # Try simple date format
                return datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                pass

        return value


def parse_jql(jql_string, user=None):
    """
    Convenience function to parse JQL.

    Args:
        jql_string: The JQL query string
        user: The current user (optional)

    Returns:
        Django Q object
    """
    parser = JQLParser()
    return parser.parse(jql_string, user)
