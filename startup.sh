#!/usr/bin/env bash
# startup.sh — Universal launcher for Linux/VPS/justrunmy.app
# Just run: bash startup.sh
# Everything is handled automatically.

echo "========================================"
echo " Netflix Checker Bot — startup.sh"
echo "========================================"

# Ensure python3 / python is available
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python not found. Please install Python 3.8+"
    exit 1
fi
echo "Using Python: $PYTHON"

# Hand off to main.py which does all installs + starts the bot
exec "$PYTHON" main.py

