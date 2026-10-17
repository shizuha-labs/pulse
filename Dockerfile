FROM python:3.10-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV UV_PYTHON_DOWNLOADS=0
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:$PATH"

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        jq \
        build-essential \
        libpq-dev \
        netcat-traditional \
        supervisor \
        vim \
        curl \
        wget

# Install Python dependencies from the locked uv project metadata.
# pyproject.toml + uv.lock are the single source of truth (PLAT-1302). The old
# floor-range requirements.txt was removed — it was migration input only, unused
# by this build, and its open `>=` floors made osv resolve to the range minimum
# (false-positive CVE flood); uv.lock pins exact, verifiable resolved versions.
COPY pyproject.toml uv.lock /app/
# Install strictly FROM the lockfile with --frozen (never re-resolve). `--locked`
# re-resolves against the live index and fails "lockfile needs to be updated"
# intermittently when the build's index/network view differs from where the lock
# was generated — this flapped every shizuha-tasks deploy 2026-07-03/04 while a
# clean host run of the identical files+uv passed (the lock was valid; the flake
# was network-dependent re-resolution). --frozen installs exactly what's locked,
# deterministic and offline-safe. Lock-vs-pyproject DRIFT is caught upstream in the
# ci-test gate (uv lock --check), so this doesn't lose drift protection. uv docs
# recommend --frozen for reproducible container builds.
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# Copy project
COPY . /app/

# Create directories for logs
RUN mkdir -p /var/log/django /var/log/supervisor

# Collect static files
RUN python manage.py collectstatic --noinput || true

# Create entrypoint script
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# Copy supervisord configuration
COPY supervisord.conf /app/

# Expose ports (8002 = Django API, 18002 = private probe, 18101 = MCP SSE daemon)
EXPOSE 8002 18002 18101

# Use entrypoint script
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]

# PLAT-10781: rebuild-trigger after python:3.10-slim base resnapshot (no behavioral change).
