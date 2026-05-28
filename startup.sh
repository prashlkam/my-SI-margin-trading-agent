#!/bin/bash
# Azure App Service Linux startup script for PLK SI Magin Trading Agent
# This script is executed by Azure App Service when the container starts.

# Install dependencies
pip install -r requirements.txt

# Start the application using gunicorn with uvicorn workers
# Azure App Service sets the PORT environment variable automatically.
gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 1 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
