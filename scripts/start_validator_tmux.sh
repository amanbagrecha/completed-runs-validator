#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8123}"
SESSION="${COMPLTD_TMUX_SESSION:-compltd-validator}"
HOST="${COMPLTD_PUBLIC_HOST:-}"

if [[ -z "$HOST" ]]; then
  HOST="$(hostname -I | cut -d' ' -f1)"
fi
if [[ -z "$HOST" ]]; then
  HOST="127.0.0.1"
fi

uv sync

SERVER_CMD="uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux send-keys -t "$SESSION" C-c
  tmux send-keys -t "$SESSION" "$SERVER_CMD" C-m
else
  tmux new-session -d -s "$SESSION" "$SERVER_CMD"
fi

echo "Started tmux session: ${SESSION}"
echo "Local URL: http://127.0.0.1:${PORT}/review"
echo "Network URL: http://${HOST}:${PORT}/review"
echo "Attach with: tmux attach -t ${SESSION}"
