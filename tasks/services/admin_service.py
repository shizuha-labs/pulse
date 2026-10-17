"""
Admin service for communication with shizuha-admin.

This service handles all organization and project management operations
by calling the shizuha-admin internal API. This centralizes org/project
management in shizuha-admin instead of having duplicate data in shizuha-pulse.
"""
import requests
import logging
from django.conf import settings
from django.core.cache import cache
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Cache timeouts
ORG_CACHE_TIMEOUT = 300  # 5 minutes
MEMBERSHIP_CACHE_TIMEOUT = 60  # 1 minute


class AdminServiceUnavailable(Exception):
    """shizuha-admin could not be reached or did not answer authoritatively.

    Raised by the strict read variants (``_make_request_strict`` and friends)
    when an upstream failure makes the answer *unknown* rather than
    authoritatively negative. Callers that serve authorization-relevant
    projections must fail closed on this instead of degrading to a short
    answer (PLS-728).
    """


class AdminService:
    """
    Service to communicate with shizuha-admin for organization and project management.

    All organization data is now managed by shizuha-admin. This service provides
    methods to query that data from shizuha-pulse.
    """

    def __init__(self):
        self.admin_api_url = getattr(
            settings,
            'SHIZUHA_ADMIN_API_URL',
            'http://shizuha-admin:8003/api'
        )
        self.internal_api_url = f"{self.admin_api_url}/internal"
        self.timeout = 10
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Dict = None,
        data: Dict = None,
        internal: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Make HTTP request to shizuha-admin API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            data: Request body data
            internal: Use internal API URL (default True)

        Returns:
            Response data as dict or None on error
        """
        base_url = self.internal_api_url if internal else self.admin_api_url
        url = f"{base_url}{endpoint}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=data,
                timeout=self.timeout
            )

            if response.status_code in (200, 201):
                # PLS-244/PLS-182: a 200 with a malformed/non-JSON body (a proxy
                # error page, a truncated response, an HTML 5xx served with a 200
                # by an intermediary) makes response.json() raise ValueError
                # (JSONDecodeError). That is NOT a requests.RequestException, so
                # it escaped the handlers below and propagated all the way into
                # ProjectSerializer.get_organization_slug/name -> a 500 on the
                # whole /api/projects/ list, breaking agent project-id discovery.
                # Treat an unparseable body as "no data" (None), same as any other
                # upstream failure, so a transient admin blip degrades the org
                # enrichment to empty instead of 500-ing the caller.
                try:
                    return response.json()
                except ValueError:
                    logger.error(
                        f"Admin API returned unparseable JSON: {method} {url} -> "
                        f"status={response.status_code}, body[:200]={response.text[:200]!r}"
                    )
                    return None
            elif response.status_code == 404:
                logger.debug(f"Resource not found: {url}")
                return None
            else:
                logger.warning(
                    f"Admin API request failed: {method} {url} -> {response.status_code}"
                )
                return None

        except requests.Timeout:
            logger.error(f"Admin API timeout: {method} {url}")
            return None
        except requests.RequestException as e:
            logger.error(f"Admin API error: {method} {url} -> {e}")
            return None

    def _make_request_strict(
        self,
        method: str,
        endpoint: str,
        params: Dict = None,
        data: Dict = None,
        internal: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Like ``_make_request`` but raises ``AdminServiceUnavailable`` on failure.

        The non-strict variant returns ``None`` for every failure mode (timeout,
        transport error, non-200, unparseable body), which is correct for
        enrichment reads that may degrade to empty. It is NOT correct for
        authorization-relevant reads: there, ``None`` is indistinguishable from
        an authoritative "no", and a caller that treats it as "no" serves a
        confident wrong answer (PLS-728). This variant preserves the
        authoritative 404 (returns ``None``) but raises on every unknown state.
        """
        base_url = self.internal_api_url if internal else self.admin_api_url
        url = f"{base_url}{endpoint}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=data,
                timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise AdminServiceUnavailable(
                f"admin timeout: {method} {url}"
            ) from exc
        except requests.RequestException as exc:
            raise AdminServiceUnavailable(
                f"admin error: {method} {url} -> {exc}"
            ) from exc

        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError as exc:
                raise AdminServiceUnavailable(
                    f"admin unparseable body: {method} {url} "
                    f"status={response.status_code}"
                ) from exc
        if response.status_code == 404:
            # Authoritative "not found" — the resource genuinely does not exist.
            return None
        raise AdminServiceUnavailable(
            f"admin HTTP {response.status_code}: {method} {url}"
        )

    # =========================================================================
    # Organization Methods
    # =========================================================================

    def get_organization(self, org_id: int) -> Optional[Dict[str, Any]]:
        """
        Get organization by ID.

        Args:
            org_id: Organization ID

        Returns:
            Organization data dict or None if not found
        """
        cache_key = f"admin_org_{org_id}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        result = self._make_request('GET', f'/organizations/{org_id}/')
        if result:
            cache.set(cache_key, result, ORG_CACHE_TIMEOUT)
        return result

    def get_organization_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """
        Get organization by slug.

        Args:
            slug: Organization slug

        Returns:
            Organization data dict or None if not found
        """
        cache_key = f"admin_org_slug_{slug}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        result = self._make_request('GET', f'/organizations/{slug}/')
        if result:
            cache.set(cache_key, result, ORG_CACHE_TIMEOUT)
            # Also cache by ID
            if result.get('id'):
                cache.set(f"admin_org_{result['id']}", result, ORG_CACHE_TIMEOUT)
        return result

    def get_user_organizations(
        self,
        user_id: int,
        email: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get all organizations a user belongs to.

        Args:
            user_id: User ID
            email: Optional user email for additional matching

        Returns:
            List of organization dicts
        """
        params = {}
        if email:
            params['email'] = email

        result = self._make_request(
            'GET',
            f'/users/{user_id}/organizations/',
            params=params
        )

        if result and 'organizations' in result:
            return result['organizations']
        return []

    def get_user_organization_ids(
        self,
        user_id: int,
        email: str = None
    ) -> List[int]:
        """
        Get IDs of all organizations a user belongs to.

        Args:
            user_id: User ID
            email: Optional user email

        Returns:
            List of organization IDs
        """
        orgs = self.get_user_organizations(user_id, email)
        return [org['id'] for org in orgs]

    # =========================================================================
    # Membership Methods
    # =========================================================================

    def check_org_membership(
        self,
        user_id: int,
        org_id: int,
        email: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Check if user is a member of an organization.

        Args:
            user_id: User ID
            org_id: Organization ID
            email: Optional user email for additional matching

        Returns:
            Membership info dict or None if not a member
        """
        cache_key = f"admin_membership_{org_id}_{user_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached if cached else None

        params = {}
        if email:
            params['email'] = email

        result = self._make_request(
            'GET',
            f'/organizations/{org_id}/members/{user_id}/',
            params=params
        )

        if result and result.get('is_member'):
            cache.set(cache_key, result, MEMBERSHIP_CACHE_TIMEOUT)
            return result
        else:
            # Cache negative result for shorter time
            cache.set(cache_key, False, 30)
            return None

    def is_org_member(
        self,
        user_id: int,
        org_id: int,
        email: str = None
    ) -> bool:
        """
        Check if user is a member of an organization (boolean).

        Args:
            user_id: User ID
            org_id: Organization ID
            email: Optional user email

        Returns:
            True if member, False otherwise
        """
        membership = self.check_org_membership(user_id, org_id, email)
        return membership is not None and membership.get('is_member', False)

    def check_org_membership_strict(
        self,
        user_id: int,
        org_id: int,
        email: str = None
    ) -> Optional[Dict[str, Any]]:
        """Authoritative org-membership check; raises ``AdminServiceUnavailable`` when unknown.

        Mirrors ``check_org_membership`` but uses ``_make_request_strict`` so an
        unreachable/unresponsive shizuha-admin raises instead of degrading to a
        cached-or-``None`` negative. A 404 (authoritative not-found) still
        returns ``None``. Used by authorization-relevant projections (PLS-728)
        where a short answer must never be served as if it were authoritative.
        """
        cache_key = f"admin_membership_{org_id}_{user_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached if cached else None

        params = {}
        if email:
            params['email'] = email

        result = self._make_request_strict(
            'GET',
            f'/organizations/{org_id}/members/{user_id}/',
            params=params
        )

        if result and result.get('is_member'):
            cache.set(cache_key, result, MEMBERSHIP_CACHE_TIMEOUT)
            return result
        # Authoritative negative (admin answered "not a member").
        cache.set(cache_key, False, 30)
        return None

    def is_org_member_strict(
        self,
        user_id: int,
        org_id: int,
        email: str = None
    ) -> bool:
        """Like ``is_org_member`` but raises ``AdminServiceUnavailable`` when unknown."""
        membership = self.check_org_membership_strict(user_id, org_id, email)
        return membership is not None and membership.get('is_member', False)

    def can_manage_org(
        self,
        user_id: int,
        org_id: int,
        email: str = None
    ) -> bool:
        """
        Check if user can manage an organization (owner or admin role).

        Args:
            user_id: User ID
            org_id: Organization ID
            email: Optional user email

        Returns:
            True if user can manage org, False otherwise
        """
        membership = self.check_org_membership(user_id, org_id, email)
        if not membership or not membership.get('is_member'):
            return False
        return membership.get('role') in ['owner', 'admin']

    def get_org_members(
        self,
        org_id: int,
        search: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get organization members, optionally filtered by search query.

        Args:
            org_id: Organization ID
            search: Optional search term to filter by email or name

        Returns:
            List of member dicts with user_id, user_email, user_name, role
        """
        cache_key = f"admin_org_members_{org_id}"
        if search:
            cache_key = f"{cache_key}_{search.lower()[:20]}"

        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        params = {}
        if search:
            params['search'] = search

        members = []
        page = 1
        while page <= 100:
            page_params = dict(params)
            if page > 1:
                page_params['page'] = page
            result = self._make_request(
                'GET',
                f'/organizations/{org_id}/members/',
                params=page_params,
            )
            if not result:
                break
            if isinstance(result, list):
                members.extend(result)
                break
            if 'results' in result:
                members.extend(result.get('results') or [])
                if not result.get('next'):
                    break
                page += 1
                continue
            if 'members' in result:
                members.extend(result.get('members') or [])
            break

        # Cache for 1 minute
        cache.set(cache_key, members, MEMBERSHIP_CACHE_TIMEOUT)
        return members

    # =========================================================================
    # Permission Check Methods
    # =========================================================================

    def check_permission(
        self,
        user_id: int,
        resource_type: str,
        resource_id: int,
        action: str = 'browse'
    ) -> Dict[str, Any]:
        """
        Check permission using shizuha-admin's permission service.

        Args:
            user_id: User ID
            resource_type: 'organization' or 'project'
            resource_id: ID of the resource
            action: Action to check (browse, create, edit, delete, manage)

        Returns:
            Dict with 'allowed' boolean and optional 'reason'
        """
        result = self._make_request(
            'POST',
            '/permissions/check/',
            data={
                'user_id': user_id,
                'resource_type': resource_type,
                'resource_id': resource_id,
                'action': action
            }
        )

        if result:
            return result
        return {'allowed': False, 'reason': 'Failed to check permission'}

    # =========================================================================
    # Project Methods (Projects are managed in shizuha-admin)
    # =========================================================================

    def get_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        """
        Get project by ID.

        Args:
            project_id: Project ID

        Returns:
            Project data dict or None if not found
        """
        return self._make_request('GET', f'/projects/{project_id}/')

    def get_user_projects(
        self,
        user_id: int,
        organization_id: int = None,
        email: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get all projects a user can access.

        Args:
            user_id: User ID
            organization_id: Optional filter to specific organization
            email: Optional user email

        Returns:
            List of project dicts
        """
        params = {}
        if organization_id:
            params['organization_id'] = organization_id
        if email:
            params['email'] = email

        result = self._make_request(
            'GET',
            f'/users/{user_id}/projects/',
            params=params
        )

        if result and 'projects' in result:
            return result['projects']
        return []

    def get_next_item_key(self, project_id: int) -> Optional[Dict[str, Any]]:
        """
        Get next item key for a project (atomic operation).

        Args:
            project_id: Project ID

        Returns:
            Dict with item_key, project_key, organization_id or None
        """
        return self._make_request(
            'POST',
            f'/projects/{project_id}/next-item-key/'
        )

    # =========================================================================
    # Cache Management
    # =========================================================================

    def invalidate_org_cache(self, org_id: int = None, slug: str = None):
        """
        Invalidate organization cache.

        Args:
            org_id: Organization ID to invalidate
            slug: Organization slug to invalidate
        """
        if org_id:
            cache.delete(f"admin_org_{org_id}")
        if slug:
            cache.delete(f"admin_org_slug_{slug}")

    def invalidate_membership_cache(self, org_id: int, user_id: int):
        """
        Invalidate membership cache for a user/org combination.

        Args:
            org_id: Organization ID
            user_id: User ID
        """
        cache.delete(f"admin_membership_{org_id}_{user_id}")


# Global instance
admin_service = AdminService()
