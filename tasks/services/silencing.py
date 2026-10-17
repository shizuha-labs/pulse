"""
Silencing Service

Evaluates silence rules to determine if notifications should be suppressed for items.
Works across all modes (task, action, alert).
"""

import fnmatch
import logging
from typing import Optional
from django.utils import timezone

from tasks.models import Item, SilenceRule

logger = logging.getLogger(__name__)


class SilencingService:
    """
    Service for evaluating silence rules and managing silencing.
    """

    @classmethod
    def is_silenced(cls, item: Item) -> bool:
        """
        Check if an item is silenced (either directly or by a rule).

        Args:
            item: The Item to check

        Returns:
            True if the item should be silenced
        """
        # Check direct silencing on the item
        if item.silenced_until and timezone.now() < item.silenced_until:
            return True

        # Check silence rules
        matching_rule = cls.find_matching_rule(item)
        return matching_rule is not None

    @classmethod
    def find_matching_rule(cls, item: Item) -> Optional[SilenceRule]:
        """
        Find a silence rule that matches the given item.

        Args:
            item: The Item to match against rules

        Returns:
            Matching SilenceRule or None
        """
        now = timezone.now()

        # Get all active rules
        rules = SilenceRule.objects.filter(is_active=True)

        for rule in rules:
            if cls._rule_matches(rule, item, now):
                # Update rule stats
                rule.match_count += 1
                rule.last_matched_at = now
                rule.save(update_fields=['match_count', 'last_matched_at'])

                logger.debug(f"Item {item.id} matched silence rule {rule.name}")
                return rule

        return None

    @classmethod
    def _rule_matches(cls, rule: SilenceRule, item: Item, now=None) -> bool:
        """
        Check if a silence rule matches an item.
        """
        if now is None:
            now = timezone.now()

        # Check time bounds
        if rule.starts_at and now < rule.starts_at:
            return False
        if rule.ends_at and now > rule.ends_at:
            return False

        # Check mode
        if rule.modes and item.mode not in rule.modes:
            return False

        # Check item_types (supports wildcards)
        if rule.item_types:
            if not any(cls._type_matches(pattern, item.item_type) for pattern in rule.item_types):
                return False

        # Check sources
        if rule.sources and item.source not in rule.sources:
            return False

        # Check severities (for alerts)
        if rule.severities and item.severity and item.severity not in rule.severities:
            return False

        # Check label matchers
        if rule.label_matchers:
            if not cls._labels_match(rule.label_matchers, item.labels):
                return False

        # TODO: Implement JSON Logic conditions evaluation
        # if rule.conditions:
        #     if not jsonlogic.evaluate(rule.conditions, item_data):
        #         return False

        # TODO: Handle recurring silences
        # if rule.is_recurring:
        #     if not cls._is_in_recurrence_window(rule, now):
        #         return False

        return True

    @classmethod
    def _type_matches(cls, pattern: str, item_type: str) -> bool:
        """
        Check if an item_type matches a pattern (supports wildcards).

        Examples:
            "alert.*" matches "alert.error", "alert.warning"
            "action.hedging.*" matches "action.hedging.short", "action.hedging.long"
            "task.reminder" matches only "task.reminder"
        """
        return fnmatch.fnmatch(item_type, pattern)

    @classmethod
    def _labels_match(cls, matchers: dict, labels: dict) -> bool:
        """
        Check if all label matchers match the item's labels.
        """
        for key, expected_value in matchers.items():
            actual_value = labels.get(key)

            # Support regex/wildcard matching for label values
            if isinstance(expected_value, str) and ('*' in expected_value or '?' in expected_value):
                if not actual_value or not fnmatch.fnmatch(str(actual_value), expected_value):
                    return False
            else:
                if actual_value != expected_value:
                    return False

        return True

    @classmethod
    def silence_item(
        cls,
        item: Item,
        duration_minutes: int = None,
        until: 'datetime' = None,
        silenced_by: str = 'manual',
        reason: str = ''
    ) -> Item:
        """
        Silence an individual item.

        Args:
            item: The item to silence
            duration_minutes: Duration in minutes (alternative to until)
            until: Specific datetime to silence until
            silenced_by: Who/what is silencing
            reason: Reason for silencing

        Returns:
            The updated item
        """
        if until is None:
            if duration_minutes:
                until = timezone.now() + timezone.timedelta(minutes=duration_minutes)
            else:
                raise ValueError("Either duration_minutes or until must be provided")

        item.silenced_until = until
        item.silenced_by = silenced_by
        item.silence_reason = reason
        item.save()

        logger.info(f"Item {item.id} silenced until {until} by {silenced_by}")
        return item

    @classmethod
    def create_silence_rule(
        cls,
        name: str,
        duration_minutes: int = None,
        ends_at: 'datetime' = None,
        modes: list = None,
        item_types: list = None,
        sources: list = None,
        severities: list = None,
        label_matchers: dict = None,
        created_by: str = 'manual',
        reason: str = ''
    ) -> SilenceRule:
        """
        Create a new silence rule.

        Args:
            name: Rule name
            duration_minutes: Duration in minutes (alternative to ends_at)
            ends_at: Specific datetime when silence expires
            modes: List of modes to match ['task', 'action', 'alert']
            item_types: List of item_type patterns
            sources: List of sources
            severities: List of severities
            label_matchers: Dict of label key/value matchers
            created_by: Who created this rule
            reason: Reason for the silence

        Returns:
            The created SilenceRule
        """
        now = timezone.now()

        if ends_at is None and duration_minutes:
            ends_at = now + timezone.timedelta(minutes=duration_minutes)

        rule = SilenceRule.objects.create(
            name=name,
            modes=modes or [],
            item_types=item_types or [],
            sources=sources or [],
            severities=severities or [],
            label_matchers=label_matchers or {},
            starts_at=now,
            ends_at=ends_at,
            created_by=created_by,
            reason=reason,
            is_active=True
        )

        logger.info(f"Silence rule created: {rule.name} (expires: {ends_at})")
        return rule

    @classmethod
    def get_active_silences(cls) -> list[SilenceRule]:
        """
        Get all currently active silence rules.
        """
        now = timezone.now()
        return list(
            SilenceRule.objects.filter(is_active=True).filter(
                # No start time or started
                models.Q(starts_at__isnull=True) | models.Q(starts_at__lte=now)
            ).filter(
                # No end time or not ended
                models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=now)
            )
        )

    @classmethod
    def cleanup_expired_silences(cls) -> int:
        """
        Deactivate expired silence rules.
        Should be called periodically by a Celery task.

        Returns:
            Number of rules deactivated
        """
        now = timezone.now()
        expired = SilenceRule.objects.filter(
            is_active=True,
            ends_at__lt=now
        )

        count = expired.count()
        expired.update(is_active=False)

        if count > 0:
            logger.info(f"Deactivated {count} expired silence rules")

        return count


# Import models here to avoid circular import
from django.db import models
