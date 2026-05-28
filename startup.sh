#!/bin/bash
# Azure App Service Linux startup script for PLK SI Magin Trading Agent
# This script is executed by Azure App Service when the container starts.

# Exit on any error
set -e

# Enable unbuffered Python logging for real-time Azure log streaming
export PYTHONUNBUFFERED=1

# Azure persistent storage directory (survives restarts)
export DATA_DIR=/home/site/data

# Install dependencies
pip install -r requirements.txt --no-cache-dir

# Start the application using gunicorn with uvicorn workers.
# Azure App Service sets the PORT environment variable automatically.
#
# IMPORTANT: Workers is set to 1 because the app uses:
#   - Shared in-memory state (active broker, simulation clock)
#   - Background async tasks (trading_agent_loop, backtesting)
# Multiple workers would cause state fragmentation.
exec gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:"${PORT:-8000}" \
    --workers 1 \
    --timeout 300 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
