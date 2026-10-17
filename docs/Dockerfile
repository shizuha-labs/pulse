# Dockerfile for Sphinx documentation with live reload
FROM python:3.10-slim

WORKDIR /docs

# Install system dependencies
RUN apt-get update && apt-get install -y \
    make \
    graphviz \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY docs/requirements.txt /docs/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application requirements for Django setup
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Set environment for Django
ENV DJANGO_SETTINGS_MODULE=taskmanager.settings
ENV PYTHONPATH=/app

EXPOSE 8080

# Default command - live reload server
CMD ["sphinx-autobuild", "source", "_build/html", \
     "--port", "8080", \
     "--host", "0.0.0.0", \
     "--watch", "/app/tasks", \
     "--ignore", "*.pyc", \
     "--ignore", "__pycache__/*"]
