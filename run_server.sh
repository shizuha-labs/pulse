#!/bin/bash
# Start the Django server for shizuha-pulse on the custom port
cd "$(dirname "$0")"
python manage.py runserver 0.0.0.0:8002