"""
User service integration for fetching user information.

Architecture:
- shizuha-id: Source of truth for user identity (email, username, profile)
- shizuha-admin: Source of truth for organizations, teams, memberships
- shizuha-agent: Source of truth for AI agent info
- shizuha-id internal push API: Used for task push notifications
"""
import logging
import threading
import time
from collections import OrderedDict

import requests
from django.conf import settings
from django.core.cache import cache
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

AGENT_IDENTITY_CACHE_TIMEOUT = 60
AGENT_IDENTITY_ERROR_CACHE_TIMEOUT = 15

# PLS-472: bounded last-known-good identity cache. A transient shizuha-id lookup
# timeout must not silently return None and orphan otherwise routable team work.
# On timeout we serve the last-known-good result (with freshness telemetry);
# security/review authority elevation callers pass allow_stale=False (default)
# so stale data is never used to elevate.
_IDENTITY_CACHE_MAX = 4096
_IDENTITY_CACHE_TTL_SECONDS = 300
_IDENTITY_CACHE_STALE_GRACE_SECONDS = 900


class _BoundedIdentityCache:
    """Thread-safe, bounded, TTL'd last-known-good identity cache (PLS-472)."""

    def __init__(self, max_entries: int = _IDENTITY_CACHE_MAX, ttl: int = _IDENTITY_CACHE_TTL_SECONDS):
        self._max = max_entries
        self._ttl = ttl
        self._data: "OrderedDict[str, tuple[float, Optional[Dict[str, Any]]]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, now: float | None = None) -> Optional[Dict[str, Any]]:
        """Return the cached identity if fresh (within TTL), else None.

        Also returns None for an explicitly cached miss (identity known absent)
        so a 404 doesn't flap into a phantom user on the next timeout.
        """
        now = now if now is not None else time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            cached_at, identity = entry
            if now - cached_at > self._ttl:
                return None
            return identity

    def get_stale(self, key: str, now: float | None = None) -> Optional[Dict[str, Any]]:
        """Return the cached identity within the stale-grace window (PLS-472).

        Used only for the last-known-good fallback on a shizuha-id outage; never
        for authority elevation. Returns None for an explicitly cached miss.
        """
        now = now if now is not None else time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            cached_at, identity = entry
            if identity is None:
                return None
            if now - cached_at > _IDENTITY_CACHE_STALE_GRACE_SECONDS:
                return None
            return identity

    def stale_age(self, key: str, now: float | None = None) -> Optional[float]:
        """Age in seconds of the cached entry (None if absent/expired)."""
        now = now if now is not None else time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            cached_at, _identity = entry
            return now - cached_at

    def set(self, key: str, identity: Optional[Dict[str, Any]], now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._data[key] = (now, identity)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class UserService:
    """
    Service to fetch user information from shizuha services.

    Data sources:
    - Regular users: shizuha-id (source of truth for identity)
    - AI agents: shizuha-agent (for *@agents.shizuha.io emails)
    - Notifications: shizuha-id internal push API
    """

    def __init__(self):
        self.id_api_url = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
        self.agent_api_url = getattr(settings, 'SHIZUHA_AGENT_API_URL', '')
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # PLS-472: bounded last-known-good cache for identity lookups.
        self._identity_cache = _BoundedIdentityCache()
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Get user information by ID from shizuha-id internal API.
        """
        try:
            response = self.session.get(
                f"{self.id_api_url}/internal/users/{user_id}/",
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.info(f"User {user_id} not found in shizuha-id")
                return None
            else:
                logger.warning(f"Failed to get user {user_id}: {response.status_code}")
                return None
        except requests.RequestException as e:
            logger.error(f"Error fetching user {user_id}: {e}")
            return None
    
    def get_user_by_email(self, email: str, *, allow_stale: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get user information by email from shizuha-id (source of truth for user identity).

        Checks multiple sources:
        1. Query shizuha-id internal API for the supplied email.
        2. For agent alias domains, retry the paired @shizuha.com /
           @agents.shizuha.io address.
        3. Legacy fallback: for @agents.shizuha.io, query shizuha-agent if
           SHIZUHA_AGENT_API_URL is configured.

        PLS-472: on a transient shizuha-id lookup timeout/error, serve the
        bounded last-known-good result (with fail-loud freshness telemetry)
        instead of silently returning None and orphaning routable team work.
        ``allow_stale=True`` opts into that fallback; security/review authority
        elevation callers keep the default ``allow_stale=False`` so stale data
        is never used to elevate.
        """
        if not email:
            return None

        normalized = email.strip().lower()
        if not normalized:
            return None

        cache_key = f'identity:email:{normalized}'
        cached = self._identity_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            found = self._get_id_user_by_email(normalized)
            if found:
                self._identity_cache.set(cache_key, found)
                return found

            alias = self._agent_email_alias(normalized)
            if alias:
                found = self._get_id_user_by_email(alias)
                if found:
                    self._identity_cache.set(cache_key, found)
                    return found

            # Legacy shizuha-agent fallback. Current fleet accounts are
            # provisioned directly in shizuha-id as @agents.shizuha.io users,
            # but keep this path for older deployments.
            if normalized.endswith('@agents.shizuha.io'):
                found = self._get_agent_by_email(normalized)
                if found:
                    self._identity_cache.set(cache_key, found)
                    return found

            self._identity_cache.set(cache_key, None)
            logger.info(f"User not found in shizuha-id: {normalized}")
            return None
        except requests.RequestException:
            # PLS-472: transient shizuha-id outage — serve last-known-good with
            # fail-loud freshness telemetry rather than orphaning the lookup.
            stale = self._identity_cache.get_stale(cache_key)
            if stale is not None and allow_stale:
                age = self._identity_cache.stale_age(cache_key)
                logger.warning(
                    'PLS-472 serving last-known-good identity for %s (stale age %.0fs) '
                    'after shizuha-id lookup failure',
                    normalized,
                    age if age is not None else -1,
                )
                return stale
            logger.error(
                'PLS-472 shizuha-id lookup failed for %s and no fresh/stale identity '
                'available (allow_stale=%s)',
                normalized,
                allow_stale,
            )
            return None

    @staticmethod
    def _agent_email_alias(email: str) -> Optional[str]:
        """Return the paired agent/company alias for known agent domains."""
        if email.endswith('@shizuha.com'):
            return email[:-len('@shizuha.com')] + '@agents.shizuha.io'
        if email.endswith('@agents.shizuha.io'):
            return email[:-len('@agents.shizuha.io')] + '@shizuha.com'
        return None

    def _get_id_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Query shizuha-id by exact email. Returns None for 404/not-found.

        PLS-472: a transient timeout/connection error is re-raised (not
        swallowed as "not found") so the caller can serve the last-known-good
        identity instead of orphaning routable work.
        """
        response = self.session.get(
            f"{self.id_api_url}/internal/users/by-email/",
            params={"email": email},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            return None
        else:
            logger.warning(f"Failed to get user by email {email}: {response.status_code}")
            raise requests.RequestException(
                f"shizuha-id by-email returned {response.status_code} for {email}"
            )

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Resolve a username (@handle) to a user via shizuha-id internal API (PLS-100)."""
        if not username:
            return None
        try:
            response = self.session.get(
                f"{self.id_api_url}/internal/users/by-username/",
                params={"username": username},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.info(f"User not found in shizuha-id by username: {username}")
                return None
            else:
                logger.warning(f"Failed to get user by username {username}: {response.status_code}")
                return None
        except requests.RequestException as e:
            logger.error(f"Error fetching user by username {username}: {e}")
            return None

    def get_agent_identity_index(self) -> Optional[Dict[str, set]]:
        """Return the ID-authoritative set of identities classified as agents.

        Organization membership and project membership describe access roles;
        neither changes an identity's account type. Assignment projections use
        this index to keep human organization members while requiring agents to
        have an exact organization-owned TeamRoster binding.

        ``None`` means the identity service was unavailable or malformed. Callers
        must fail closed instead of treating an unknown identity as human.
        """
        cache_key = 'shizuha_id_agent_identity_index_v1'
        cached = cache.get(cache_key)
        if cached is False:
            return None
        if isinstance(cached, dict):
            return {
                'user_ids': set(cached.get('user_ids') or []),
                'emails': set(cached.get('emails') or []),
            }

        try:
            response = self.session.get(
                f"{self.id_api_url}/internal/agents/list/",
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to load agent identity inventory: %s",
                    response.status_code,
                )
                cache.set(
                    cache_key,
                    False,
                    AGENT_IDENTITY_ERROR_CACHE_TIMEOUT,
                )
                return None
            payload = response.json()
            agents = payload.get('agents') if isinstance(payload, dict) else None
            if not isinstance(agents, list):
                raise ValueError('agent identity inventory missing agents list')

            user_ids = set()
            emails = set()
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                try:
                    user_ids.add(int(agent.get('user_id')))
                except (TypeError, ValueError):
                    pass
                email = str(agent.get('email') or '').strip().lower()
                if email:
                    emails.add(email)
                    alias = self._agent_email_alias(email)
                    if alias:
                        emails.add(alias)

            cached_value = {
                'user_ids': sorted(user_ids),
                'emails': sorted(emails),
            }
            cache.set(
                cache_key,
                cached_value,
                AGENT_IDENTITY_CACHE_TIMEOUT,
            )
            return {
                'user_ids': user_ids,
                'emails': emails,
            }
        except (requests.RequestException, ValueError) as exc:
            logger.error("Error loading agent identity inventory: %s", exc)
            cache.set(
                cache_key,
                False,
                AGENT_IDENTITY_ERROR_CACHE_TIMEOUT,
            )
            return None

    def _get_agent_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get agent information by email from shizuha-agent service.

        For agent emails (*@agents.shizuha.io), we query shizuha-agent
        internal API to verify the agent actually exists before returning user info.

        Note: Agents use @agents.shizuha.io internally but their shizuha-id
        user account uses @shizuha.com (e.g., sara@shizuha.com for sara agent).
        """
        if not email or not email.endswith('@agents.shizuha.io'):
            return None

        if not self.agent_api_url:
            logger.debug("Legacy shizuha-agent lookup is disabled for %s", email)
            return None

        # Query shizuha-agent internal API to verify the agent exists
        try:
            response = requests.get(
                f"{self.agent_api_url}/internal/agents/by-email/",
                params={"email": email},
                timeout=10
            )
            if response.status_code == 200:
                agent = response.json()
                agent_name = agent.get('name', email.split('@')[0])
                # Agent's shizuha-id user uses @shizuha.com email
                username = agent_name.lower().replace(' ', '_').replace('-', '_')
                user_email = f"{username}@shizuha.com"

                # Use user_id from agent if available (linked shizuha-id account)
                user_id = agent.get('user_id')
                return {
                    'id': user_id if user_id else f"agent-{agent.get('id')}",
                    'email': user_email,  # Use @shizuha.com email for memberships
                    'agent_email': agent.get('email', email),  # Keep original agent email
                    'username': username,
                    'full_name': agent_name,
                    'first_name': agent_name,
                    'last_name': '',
                    'is_agent': True,
                    'agent_id': agent.get('id'),
                    'user_id': user_id,
                }
            elif response.status_code == 404:
                logger.info(f"Agent not found for email {email}")
                return None
            else:
                logger.warning(f"Failed to get agent by email {email}: {response.status_code}")
                return None
        except requests.RequestException as e:
            logger.error(f"Error fetching agent by email {email}: {e}")
            return None
    
    def validate_user_exists(self, user_id: int, email: str = None) -> bool:
        """
        Validate that a user exists and email matches (if provided)
        """
        user = self.get_user_by_id(user_id)
        if not user:
            return False

        if email and user.get('email') != email:
            logger.warning(f"User {user_id} email mismatch: expected {email}, got {user.get('email')}")
            return False

        return True

    def get_user_from_id_service(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Alias for get_user_by_email (kept for backward compatibility).

        Deprecated: Use get_user_by_email instead.
        """
        return self.get_user_by_email(email)

    def resolve_user_info(self, user_id: int = None, email: str = None) -> Optional[Dict[str, Any]]:
        """
        Resolve user information by ID or email.

        PLS-859: a bare username (no ``@``) is accepted as ``assignee_email`` by
        agents (e.g. ``aoi``). Resolve it via the username index first, then try
        the conventional ``<name>@shizuha.com`` email form, before giving up.
        Without this, the assign path accepted the literal string ``aoi`` as an
        email and failed later with an opaque assignment_validation_failed.
        """
        if user_id:
            return self.get_user_by_id(user_id)
        if not email:
            return None
        raw = str(email).strip()
        if not raw:
            return None
        if '@' not in raw:
            # Strip a leading @handle form if present.
            username = raw[1:] if raw.startswith('@') else raw
            by_username = self.get_user_by_username(username)
            if by_username:
                return by_username
            # Conventional agent/human email form used across rosters.
            by_email = self.get_user_by_email(f'{username}@shizuha.com')
            if by_email:
                return by_email
            return self.get_user_by_email(f'{username}@agents.shizuha.io')
        return self.get_user_by_email(raw)
    
    def send_task_notification(self, task_data: Dict[str, Any], notification_type: str = "assignment") -> bool:
        """
        Send a task notification through the messaging service
        """
        if not task_data.get('assignee_id'):
            logger.warning("Cannot send notification: no assignee_id")
            return False
        
        # Format message + push metadata based on notification type
        if notification_type == "assignment":
            content = self._format_assignment_notification(task_data)
            push_type = "pulse_task_assignment"
            push_title = "Task Assignment"
        elif notification_type == "reminder":
            content = self._format_reminder_notification(task_data)
            push_type = "pulse_task_reminder"
            push_title = "Task Reminder"
        else:
            content = self._format_task_notification(task_data)
            push_type = "pulse_task_notification"
            push_title = "Task Notification"

        # Prepare shizuha-id push payload
        notification_payload = {
            "user_id": task_data['assignee_id'],
            "title": push_title,
            "body": content,
            "data": {
                "type": push_type,
                "screen": "PulseTab",
                "task_id": str(task_data.get('id')) if task_data.get('id') is not None else "",
                # item_key (e.g. "AT-58") drives the ori-expo deep-link to the
                # task detail page (/pulse/tasks/<key>). task_id alone routes
                # to the notifications inbox as a fallback.
                "item_key": task_data.get('item_key') or task_data.get('key') or "",
                "priority": task_data.get('priority', 'medium'),
            },
            "priority": "high",
            "channel_id": "pulse_alerts",
        }

        try:
            response = self.session.post(
                f"{self.id_api_url}/internal/push/send/",
                json=notification_payload,
                headers={
                    "X-Internal-Service": "shizuha-pulse",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )

            if response.status_code in [200, 201]:
                logger.info(f"Task {notification_type} notification sent successfully for task {task_data.get('id')}")
                return True
            else:
                logger.error(f"Failed to send {notification_type} notification: {response.status_code} - {response.text}")
                return False

        except requests.RequestException as e:
            logger.error(f"Error sending task {notification_type} notification: {e}")
            return False
    
    def _format_assignment_notification(self, task_data: Dict[str, Any]) -> str:
        """
        Format task assignment notification message
        """
        task_id = task_data.get('id')
        task_title = task_data.get('title', 'Untitled Task')
        task_type = task_data.get('task_type', 'generic').replace('_', ' ').title()
        priority = task_data.get('priority', 'medium').title()
        due_date = task_data.get('due_date')
        reminder_time = task_data.get('reminder_time')
        
        message = f"📋 New {task_type} Assigned: {task_title}"
        if task_id:
            message += f" (ID: {task_id})"
        
        if priority in ['High', 'Urgent']:
            message = f"🚨 {message}"
        
        if due_date:
            message += f"\n📅 Due: {due_date}"
        
        if reminder_time:
            message += f"\n⏰ Reminder set for: {reminder_time}"
        
        if priority != 'Medium':
            message += f"\n⚡ Priority: {priority}"
        
        description = task_data.get('description')
        if description:
            desc_preview = description[:100] + '...' if len(description) > 100 else description
            message += f"\n📝 {desc_preview}"
        
        if task_id:
            message += f"\n🔗 Complete: POST /api/tasks/{task_id}/complete/"
        
        return message

    def _format_reminder_notification(self, task_data: Dict[str, Any]) -> str:
        """
        Format task reminder notification message
        """
        task_id = task_data.get('id')
        task_title = task_data.get('title', 'Untitled Task')
        due_date = task_data.get('due_date')
        priority = task_data.get('priority', 'medium').title()
        
        message = f"⏰ REMINDER: {task_title}"
        if task_id:
            message += f" (ID: {task_id})"
        
        if priority in ['High', 'Urgent']:
            message = f"🚨 {message}"
        
        if due_date:
            message += f"\n📅 Due: {due_date}"
        
        if priority != 'Medium':
            message += f"\n⚡ Priority: {priority}"
        
        description = task_data.get('description')
        if description:
            desc_preview = description[:80] + '...' if len(description) > 80 else description
            message += f"\n📝 {desc_preview}"
        
        if task_id:
            message += f"\n✅ Complete: POST /api/tasks/{task_id}/complete/"
        
        return message

    def _format_task_notification(self, task_data: Dict[str, Any]) -> str:
        """
        Format generic task notification message (fallback)
        """
        task_title = task_data.get('title', 'Untitled Task')
        task_type = task_data.get('task_type', 'generic').replace('_', ' ').title()
        priority = task_data.get('priority', 'medium').title()
        due_date = task_data.get('due_date')
        
        message = f"📋 {task_type}: {task_title}"
        
        if priority in ['High', 'Urgent']:
            message = f"🚨 {message}"
        
        if due_date:
            message += f"\n📅 Due: {due_date}"
        
        if priority != 'Medium':
            message += f"\n⚡ Priority: {priority}"
        
        description = task_data.get('description')
        if description:
            # Truncate long descriptions
            desc_preview = description[:100] + '...' if len(description) > 100 else description
            message += f"\n📝 {desc_preview}"
        
        return message


# Global instance
user_service = UserService()
