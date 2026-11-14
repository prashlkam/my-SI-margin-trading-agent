#!/bin/bash
# Azure App Service Linux startup script for PLK SI Magin Trading Agent
# This script is executed by Azure App Service when the container starts.

# Exit on any error
set -e

# Enable unbuffered Python logging for real-time Azure log streaming
export PYTHONUNBUFFERED=1

# Azure persistent storage directory (survives restarts). App Settings may override.
export DATA_DIR="${DATA_DIR:-/home/site/data}"
mkdir -p "$DATA_DIR"

# Dependencies are built at deploy time (Oryx, SCM_DO_BUILD_DURING_DEPLOYMENT=true).
# Only fall back to pip if they're missing: on the Free (F1) tier the app cold-starts
# often, and reinstalling every time burns the daily CPU quota (Azure.md §2a).
if [ -f antenv/bin/activate ]; then
    source antenv/bin/activate
fi
if ! python -c "import fastapi, uvicorn, gunicorn, pandas, fyers_apiv3" 2>/dev/null; then
    echo "Dependencies missing - installing requirements.txt"
    pip install -r requirements.txt --no-cache-dir
fi

# Start the application using gunicorn with uvicorn workers.
# Azure App Service sets the PORT environment variable automatically.
#
# IMPORTANT: Workers is set to 1 because the app uses:
#   - Shared in-memory state (active broker, simulation clock)
#   - Background async tasks (trading_agent_loop, backtesting)
# Multiple workers would cause state fragmentation - and with a live broker,
# DUPLICATE REAL ORDERS. Never raise this, and never scale out beyond 1 instance.
exec gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:"${PORT:-8000}" \
    --workers 1 \
    --timeout 300 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
