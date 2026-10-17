from django.conf import settings
from rest_framework.pagination import PageNumberPagination


def _public_uri(url):
    """Rewrite an absolute URI built by DRF to advertise the public prefix.

    The reverse proxy rewrites the public ``/pulse/`` prefix to the internal
    ``/shizuha-pulse/`` prefix before the request reaches Django, so DRF's
    ``build_absolute_uri()`` emits ``next``/``previous`` links under
    ``/shizuha-pulse/``. Clients that followed those links verbatim would
    leave the caller's path namespace (and, with the proxy scheme bug,
    HTTPS). Normalise the advertised prefix back to the public one (PLS-612).
    """
    if not url:
        return url
    internal = getattr(settings, 'FORCE_SCRIPT_NAME', '/shizuha-pulse')
    public = getattr(settings, 'PUBLIC_API_PREFIX', '/pulse')
    if internal and public and internal != public and internal in url:
        url = url.replace(internal, public, 1)
    return url


class StandardPagination(PageNumberPagination):
    """Standard pagination that supports client-requested page_size."""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 200

    def get_next_link(self):
        return _public_uri(super().get_next_link())

    def get_previous_link(self):
        return _public_uri(super().get_previous_link())


class CommentPagination(StandardPagination):
    """Comments render as a thread; keep normal pagination shape but avoid hiding page 2."""
    page_size = 200
