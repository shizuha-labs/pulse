"""
Inhibition Service

Suppresses target alerts when source alerts are firing.
Useful for reducing noise during cascading failures.
"""

import fnmatch
import logging
from typing import Optional, List
from django.db.models import Q

from tasks.models import Item, InhibitionRule

logger = logging.getLogger(__name__)


class InhibitionService:
    """
    Service for evaluating inhibition rules.

    Inhibition rules suppress certain alerts when related "parent" alerts are firing.
    For example, if "Database down" is firing, suppress "API timeout" alerts.
    """

    @classmethod
    def is_inhibited(cls, item: Item) -> bool:
        """
        Check if an alert should be inhibited based on active inhibition rules
        and currently firing alerts.

        Args:
            item: The alert to check (must be mode='alert')

        Returns:
            True if the alert should be inhibited (suppressed)
        """
        if item.mode != 'alert':
            return False

        # Find inhibition rules where this item matches the target
        rules = InhibitionRule.objects.filter(is_active=True)

        for rule in rules:
            if cls._matches_target(rule, item):
                # Check if there's a firing source alert that would inhibit this
                if cls._has_firing_source(rule, item):
                    logger.debug(f"Alert {item.id} inhibited by rule {rule.name}")
                    return True

        return False

    @classmethod
    def _matches_target(cls, rule: InhibitionRule, item: Item) -> bool:
        """
        Check if an item matches the target criteria of an inhibition rule.
        """
        # Check item_types
        if rule.target_item_types:
            if not any(fnmatch.fnmatch(item.item_type, pattern) for pattern in rule.target_item_types):
                return False

        # Check severities
        if rule.target_severities and item.severity:
            if item.severity not in rule.target_severities:
                return False

        # Check label matchers
        if rule.target_label_matchers:
            for key, value in rule.target_label_matchers.items():
                if isinstance(value, str) and ('*' in value or '?' in value):
                    if not item.labels.get(key) or not fnmatch.fnmatch(str(item.labels.get(key)), value):
                        return False
                elif item.labels.get(key) != value:
                    return False

        return True

    @classmethod
    def _has_firing_source(cls, rule: InhibitionRule, target_item: Item) -> bool:
        """
        Check if there's a firing alert that matches the source criteria
        and would inhibit the target item.
        """
        # Build query for firing source alerts
        queryset = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged']
        )

        # Filter by source item_types
        if rule.source_item_types:
            type_q = Q()
            for pattern in rule.source_item_types:
                if '*' in pattern or '?' in pattern:
                    # Convert glob to regex for database query
                    regex = pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
                    type_q |= Q(item_type__regex=regex)
                else:
                    type_q |= Q(item_type=pattern)
            queryset = queryset.filter(type_q)

        # Filter by source severities
        if rule.source_severities:
            queryset = queryset.filter(severity__in=rule.source_severities)

        # For each potential source alert, check label matchers and equal_labels
        for source_alert in queryset:
            if cls._source_matches(rule, source_alert, target_item):
                return True

        return False

    @classmethod
    def _source_matches(cls, rule: InhibitionRule, source: Item, target: Item) -> bool:
        """
        Check if a source alert matches the source criteria and
        equal_labels constraints with the target.
        """
        # Check source label matchers
        if rule.source_label_matchers:
            for key, value in rule.source_label_matchers.items():
                if isinstance(value, str) and ('*' in value or '?' in value):
                    if not source.labels.get(key) or not fnmatch.fnmatch(str(source.labels.get(key)), value):
                        return False
                elif source.labels.get(key) != value:
                    return False

        # Check equal_labels - these labels must match between source and target
        if rule.equal_labels:
            for label_key in rule.equal_labels:
                source_value = source.labels.get(label_key)
                target_value = target.labels.get(label_key)
                if source_value != target_value:
                    return False

        return True

    @classmethod
    def get_inhibiting_alerts(cls, item: Item) -> List[Item]:
        """
        Get all alerts that are inhibiting the given item.

        Args:
            item: The alert to check

        Returns:
            List of alerts that are inhibiting this item
        """
        if item.mode != 'alert':
            return []

        inhibiting = []
        rules = InhibitionRule.objects.filter(is_active=True)

        for rule in rules:
            if cls._matches_target(rule, item):
                # Find the specific source alerts
                inhibiting.extend(cls._find_inhibiting_sources(rule, item))

        return inhibiting

    @classmethod
    def _find_inhibiting_sources(cls, rule: InhibitionRule, target_item: Item) -> List[Item]:
        """
        Find all source alerts that are inhibiting a target.
        """
        queryset = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged']
        )

        if rule.source_item_types:
            type_q = Q()
            for pattern in rule.source_item_types:
                if '*' in pattern or '?' in pattern:
                    regex = pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
                    type_q |= Q(item_type__regex=regex)
                else:
                    type_q |= Q(item_type=pattern)
            queryset = queryset.filter(type_q)

        if rule.source_severities:
            queryset = queryset.filter(severity__in=rule.source_severities)

        sources = []
        for source in queryset:
            if cls._source_matches(rule, source, target_item):
                sources.append(source)

        return sources

    @classmethod
    def get_inhibited_alerts(cls, source_alert: Item) -> List[Item]:
        """
        Get all alerts that would be inhibited by the given source alert.

        Args:
            source_alert: The alert that might be inhibiting others

        Returns:
            List of alerts that are inhibited by this source
        """
        if source_alert.mode != 'alert':
            return []

        if source_alert.status not in ['firing', 'acknowledged']:
            return []

        inhibited = []
        rules = InhibitionRule.objects.filter(is_active=True)

        for rule in rules:
            # Check if source_alert matches the source criteria
            if not cls._alert_matches_source_criteria(rule, source_alert):
                continue

            # Find target alerts that would be inhibited
            target_alerts = Item.objects.filter(
                mode='alert',
                status__in=['firing', 'acknowledged']
            ).exclude(id=source_alert.id)

            for target in target_alerts:
                if cls._matches_target(rule, target):
                    # Check equal_labels
                    if rule.equal_labels:
                        labels_match = all(
                            source_alert.labels.get(key) == target.labels.get(key)
                            for key in rule.equal_labels
                        )
                        if not labels_match:
                            continue
                    inhibited.append(target)

        return inhibited

    @classmethod
    def _alert_matches_source_criteria(cls, rule: InhibitionRule, alert: Item) -> bool:
        """
        Check if an alert matches the source criteria of an inhibition rule.
        """
        # Check item_types
        if rule.source_item_types:
            if not any(fnmatch.fnmatch(alert.item_type, pattern) for pattern in rule.source_item_types):
                return False

        # Check severities
        if rule.source_severities and alert.severity:
            if alert.severity not in rule.source_severities:
                return False

        # Check label matchers
        if rule.source_label_matchers:
            for key, value in rule.source_label_matchers.items():
                if isinstance(value, str) and ('*' in value or '?' in value):
                    if not alert.labels.get(key) or not fnmatch.fnmatch(str(alert.labels.get(key)), value):
                        return False
                elif alert.labels.get(key) != value:
                    return False

        return True
