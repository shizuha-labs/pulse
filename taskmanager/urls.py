"""
URL configuration for taskmanager project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from django.db import OperationalError
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)


def health_check(request):
    """Simple health check endpoint for container health checks."""
    return JsonResponse({'status': 'healthy'})


def data_health_check(request):
    """Data-plane health check: confirms the ORM + DB connection is alive.

    PLAT-954: the rebalance sweep gates on this endpoint (via
    _rebalance_pulse_api_health_gate) rather than the process-only /api/health/
    so that a Django process that is up but whose DB connection is broken
    (e.g. during a Postgres failover or network partition) correctly reads as
    degraded and suppresses the sweep. Unauthenticated; cheap single-row probe.
    """
    try:
        from tasks.models import Item
        Item.objects.exists()
        return JsonResponse({'status': 'healthy', 'data_plane': 'ok'})
    except Exception as exc:
        return JsonResponse(
            {'status': 'degraded', 'data_plane': 'error', 'detail': str(exc)},
            status=503,
        )

urlpatterns = [
    # Prefixed URLs to match what nginx sends
    path('shizuha-pulse/admin/', admin.site.urls),
    path('shizuha-pulse/api/', include('tasks.urls')),
    path('shizuha-pulse/api/auth/', include('tasks.auth_urls')),

    # OpenAPI schema and documentation (prefixed)
    path('shizuha-pulse/api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('shizuha-pulse/api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('shizuha-pulse/api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),

    # Also keep non-prefixed URLs for direct access
    path('admin/', admin.site.urls),
    path('api/', include('tasks.urls')),
    path('api/auth/', include('tasks.auth_urls')),
    path('api/health/', health_check, name='health-check'),
    path('api/health/data/', data_health_check, name='data-health-check'),

    # OpenAPI schema and documentation (non-prefixed)
    path('api/schema/', SpectacularAPIView.as_view(), name='schema-direct'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema-direct'), name='swagger-ui-direct'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema-direct'), name='redoc-direct'),
]

# Serve static files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
