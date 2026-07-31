#!/bin/bash
# Serve the reader locally (fetch() needs HTTP, file:// is blocked by CORS).
PORT="${1:-8000}"
cd "$(dirname "$0")"
( sleep 1; open "http://localhost:$PORT/" 2>/dev/null ) &
exec python3 -m http.server "$PORT"
