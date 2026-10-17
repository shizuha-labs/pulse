"""PLAT-5928 / PLAT-4571 slice 3 — durable Alertmanager acceptance + verified
Connect alert delivery (Appendix §8.4.2A of HLD PLAT-301).

This app owns:
- strict wire-format ingestion (RFC 8785-canonicalisable AlertEnvelopeV1),
- the Postgres-authoritative AlertIngressReceiptV1 / AlertEpisodeV1 /
  AlertRecipientPhaseV1 / AlertDeliveryOutboxV1 durable state machines,
- the fenced, leased relay that delivers VerifiedAlertDelivery packets to
  Connect and reconciles ambiguous results through the receipt lookup.
"""
from __future__ import annotations

from django.apps import AppConfig


class AlertDeliveryConfig(AppConfig):
    name = 'alertdelivery'
    default_auto_field = 'django.db.models.BigAutoField'
    verbose_name = 'Verified Alert Delivery (PLAT-4571 slice 3)'
