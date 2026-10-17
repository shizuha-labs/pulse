#!/bin/bash
set -e

# Wait for database to be ready
DB_HOST="${POSTGRES_HOST:-shizuha-db}"
DB_PORT="${POSTGRES_PORT:-5432}"
echo "Waiting for database at $DB_HOST:$DB_PORT..."
while ! nc -z "$DB_HOST" "$DB_PORT"; do
  sleep 0.1
done
echo "Database started"

# Create database if it doesn't exist using Django management command
echo "Creating database if it doesn't exist..."
python manage.py createdb

# Run migrations
echo "Running migrations..."
python manage.py migrate --noinput

# Note: Superuser creation is skipped because Pulse uses federated authentication
# with Shizuha ID. Users are authenticated via JWT tokens from shizuha-id.
# Creating local users would cause ID conflicts with the federated user IDs.
# For Django admin access, use the shizuha-id superuser credentials.
echo "Skipping local superuser creation (using federated auth with Shizuha ID)"

# Collect static files for nginx to serve
echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Setup complete. Starting services..."

# Execute the main command
# If no command specified, use supervisord (default for production)
# If command is watchfiles, it will be used directly (for development hot-reload)
exec "$@"