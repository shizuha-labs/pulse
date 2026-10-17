"""URL surface for the dedicated private probe listener only."""

from django.urls import path

from tasks.probe_views import (
    db_readiness_probe, process_liveness_probe, runtime_live_probe_receipt,
)


urlpatterns = [
    path("livez/", process_liveness_probe, name="probe-process-liveness"),
    path("api/internal/probes/db-readiness/", db_readiness_probe, name="probe-db-readiness"),
    path(
        "api/internal/runtime/live-probe-receipt/",
        runtime_live_probe_receipt,
        name="runtime-live-probe-receipt",
    ),
]
