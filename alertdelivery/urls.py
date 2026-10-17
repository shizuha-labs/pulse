"""PLAT-5928 alertdelivery URL wiring."""
from __future__ import annotations

from django.urls import path

from .views import PrometheusAlertmanagerDurableWebhookView

urlpatterns = [
    path(
        'webhooks/alertmanager-durable/<str:key>/',
        PrometheusAlertmanagerDurableWebhookView.as_view(),
        name='webhook-alertmanager-durable',
    ),
]
