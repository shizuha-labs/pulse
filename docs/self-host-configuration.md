# External deployment configuration

Pulse uses PostgreSQL, Redis and Shizuha ID. Configure `POSTGRES_DB`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `SECRET_KEY`, and
`SHIZUHA_ID_API_URL` for your installation. Generate unique credentials; never
reuse source examples. Set `DEBUG=false` on an exposed deployment.

Set `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` as comma-separated lists. For a
separate browser origin also set `CORS_ALLOWED_ORIGINS`. `CORS_ALLOW_ALL_ORIGINS`
defaults to the debug setting. Set `FORCE_SCRIPT_NAME` and `PUBLIC_API_PREFIX`
to the backend mount and public API mount respectively; empty values support
installation at the root. Defaults preserve the `/shizuha-pulse` backend and
`/pulse` public proxy convention.

Set `SHIZUHA_EXPECTED_ISSUER` and `PULSE_FLEET_READ_ISSUER` to your ID issuer.
Set `SHIZUHA_REVOCATION_STATUS_URL` to the reachable internal ID endpoint
(e.g. `http://id:8001/api/oauth/revocation-status`) independently of the public
issuer. The endpoint validates the submitted signed token. Lookup errors deny
the request without caching an authoritative revocation; active and revoked
verdicts retain their 30-second cache. Redirects are rejected.
The latter defaults to the former. Integration endpoints include
`SHIZUHA_CONNECT_API_URL`, `SHIZUHA_ADMIN_API_URL`, `SHIZUHA_CORTEX_API_URL`,
`SHIZUHA_WIKI_API_URL`, `SHIZUHA_WIKI_PUBLIC_URL`,
`SHIZUHA_ID_OAUTH_TOKEN_URL`, `ALERTMANAGER_URL`, `PROMETHEUS_URL` and `BOOKS_URL`.
Optional integrations retain unavailable/error states when not configured;
an unavailable integration must not be represented as healthy data.

For Forgejo evidence, set `ORIGIN_PUBLIC_URL` to your canonical HTTPS origin
and `ORIGIN_API_URL` to its API base. Optional comma-separated
`ORIGIN_PUBLIC_HOST_ALIASES`, `ORIGIN_INTERNAL_HOST_ALIASES` and
`ORIGIN_LEGACY_HOST_ALIASES` describe explicitly trusted deployment aliases.
`PULSE_FROZEN_DEVELOPMENT_REPOS` optionally maps retired `owner/repository`
names to an explanation as a JSON object; its default is empty.
Legacy aliases identify existing records only; they cannot authorize new PR
links. Canonical PR links retain exact scheme, host and path checks. Set
`ORIGIN_CI_LOGS_URL` to a log endpoint without query parameters if available;
unconfigured installations do not advertise a log server.

`SOURCE_AUTHORITY_CONTRACT_URL`, `PULSE_OPERATOR_EMAIL` and
`DESIGN_REVIEW_CONVERSATION_ID` have no built-in organization identity.
Configure them explicitly for the corresponding workflow integrations.
`PULSE_COS_EMAIL` names the coordinator and `PULSE_HUMAN_EMAILS` optionally
lists human accounts (defaulting to the configured operator). Historical
operator seed migrations skip their seed when no operator email is configured.
`PULSE_SUPPRESSED_THERMAL_HOSTS` optionally lists telemetry hosts suppressed by
your operating policy; no host is suppressed by default.

The attachment canary's `PULSE_EXPECTED_MINIO_ENDPOINT`,
`PULSE_EXPECTED_MINIO_BUCKET` and `PULSE_EXPECTED_MINIO_REGION` are independent
reviewed topology expectations. They deliberately do not inherit the runtime
`MINIO_*` settings: pointing the workload at the wrong storage must still fail
the detector.

The browser verification helper `frontend/ac16-verify.cjs` requires
`BASE_URL`, `SHIZUHA_ACCESS_TOKEN`, `SHIZUHA_REFRESH_TOKEN` and
`SHIZUHA_USER_JSON` in the invoking environment. Supply a test account session;
never commit session values or captured browser storage.

When the object store's internal address cannot be reached by browsers, set
`MINIO_PUBLIC_ENDPOINT` to the browser-facing host and optional port (no scheme
or path), and `MINIO_PUBLIC_SECURE=true` for HTTPS. The internal `MINIO_ENDPOINT`
continues to handle object operations and bucket-region lookup. Download and
thumbnail URLs are signed against the public host directly. Leave these unset
when internal and browser endpoints are identical; do not rewrite signed URLs.
