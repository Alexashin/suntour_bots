#!/bin/sh
set -eu

echo "[startup] applying Alembic migrations..."
alembic upgrade head

echo "[startup] starting application..."
exec python -m app.main
