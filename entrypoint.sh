#!/bin/bash
set -e

# Default to port 8000 if not set
PORT="${PORT:-8080}"

exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
