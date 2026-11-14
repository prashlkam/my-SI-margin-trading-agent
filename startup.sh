#!/bin/bash
# Azure App Service Linux startup script for PLK SI Magin Trading Agent
# This script is executed by Azure App Service when the container starts.
#
# Dependencies are NEVER reinstalled on a normal start. On the Free (F1) tier the
# app cold-starts often and every pip install burns the CPU quota (Azure.md §2a).
# Lookup order:
#   1. Packages already importable (local dev, or PYTHONPATH set by the platform)
#   2. Oryx build output: antenv/lib/python3.X/site-packages under $APP_PATH
#      (App Service extracts the build to /tmp/<id> and points APP_PATH at it),
#      the current directory, or /home/site/wwwroot
#   3. A persistent cache in /home/site/pydeps/<hash>, keyed by requirements.txt +
#      Python version. Installed ONCE if nothing else is found; reused afterwards.

# Exit on any error
set -euo pipefail

# Enable unbuffered Python logging for real-time Azure log streaming
export PYTHONUNBUFFERED=1

# Azure persistent storage directory (survives restarts). App Settings may override.
export DATA_DIR="${DATA_DIR:-/home/site/data}"
mkdir -p "$DATA_DIR"

# Run from the app directory (where the platform extracted the build, if it did).
APP_ROOT="${APP_PATH:-$(pwd)}"
cd "$APP_ROOT"

PYTHON="${PYTHON:-python3}"
PY_VER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
REQUIRED_MODULES="fastapi uvicorn gunicorn pandas numpy requests fyers_apiv3 tzdata"

# True if every required module can be found. Uses find_spec, so nothing is imported (no CPU cost).
deps_present() {
    PYTHONPATH="${1:-}${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - $REQUIRED_MODULES <<'EOF'
import importlib.util, sys
missing = [m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]
sys.exit(1 if missing else 0)
EOF
}

# fyers-apiv3 depends on the PyPI package `asyncio==3.4.3`, a Python 3.3-era backport.
# Directories on PYTHONPATH are searched BEFORE the standard library, so that copy would
# shadow the real asyncio and crash uvicorn with a SyntaxError (App Service puts the
# build's site-packages on PYTHONPATH). Remove the backport wherever it's on the path.
# Only a folder next to pip's asyncio-*.dist-info record is removed - never the stdlib.
drop_asyncio_backport() {
    local dir="$1"
    if [ -d "$dir/asyncio" ] && compgen -G "$dir/asyncio-*.dist-info" >/dev/null; then
        echo "startup: removing obsolete PyPI asyncio backport from $dir"
        rm -rf "$dir/asyncio"
    fi
}
IFS=':' read -ra PATH_ENTRIES <<< "${PYTHONPATH:-}"
for entry in "${PATH_ENTRIES[@]}"; do
    [ -n "$entry" ] && drop_asyncio_backport "$entry"
done

SITE_PACKAGES=""
if deps_present ""; then
    echo "startup: dependencies already importable"
else
    for candidate in \
        "$APP_ROOT/antenv/lib/python$PY_VER/site-packages" \
        "./antenv/lib/python$PY_VER/site-packages" \
        "/home/site/wwwroot/antenv/lib/python$PY_VER/site-packages"; do
        if [ -d "$candidate" ] && deps_present "$candidate"; then
            SITE_PACKAGES="$candidate"
            echo "startup: using build output at $SITE_PACKAGES"
            break
        fi
    done

    if [ -z "$SITE_PACKAGES" ]; then
        REQ_HASH="$( (cat requirements.txt; echo "python$PY_VER") | sha256sum | cut -c1-16)"
        CACHE_ROOT="${PYDEPS_DIR:-/home/site/pydeps}"
        CACHE_DIR="$CACHE_ROOT/$REQ_HASH"
        if [ -f "$CACHE_DIR/.complete" ] && deps_present "$CACHE_DIR"; then
            echo "startup: using cached dependencies at $CACHE_DIR"
        else
            echo "startup: no dependencies found - installing ONCE into $CACHE_DIR"
            rm -rf "$CACHE_DIR"
            mkdir -p "$CACHE_DIR"
            "$PYTHON" -m pip install --no-cache-dir --disable-pip-version-check \
                --target "$CACHE_DIR" -r requirements.txt
            touch "$CACHE_DIR/.complete"
            # Drop caches for older requirements.txt versions.
            find "$CACHE_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name "$REQ_HASH" -exec rm -rf {} + || true
        fi
        SITE_PACKAGES="$CACHE_DIR"
    fi
    drop_asyncio_backport "$SITE_PACKAGES"
    export PYTHONPATH="$SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}"
fi

# Start the application using gunicorn with uvicorn workers.
# Azure App Service sets the PORT environment variable automatically.
# `python -m gunicorn` avoids depending on a venv's bin/ scripts, whose
# interpreter paths may not exist after the platform relocates the build.
#
# IMPORTANT: Workers is set to 1 because the app uses:
#   - Shared in-memory state (active broker, simulation clock)
#   - Background async tasks (trading_agent_loop, backtesting)
# Multiple workers would cause state fragmentation - and with a live broker,
# DUPLICATE REAL ORDERS. Never raise this, and never scale out beyond 1 instance.
exec "$PYTHON" -m gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:"${PORT:-8000}" \
    --workers 1 \
    --timeout 300 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
