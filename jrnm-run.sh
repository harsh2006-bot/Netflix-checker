#!/bin/bash
cd /app/Netflix--copilot-add-bot-token-requirement-handling 2>/dev/null || \
cd /app/render_deploy 2>/dev/null || cd /app

echo "=== Netflix Bot ==="
python3 --version

pip install --quiet --no-cache-dir \
    "pyTelegramBotAPI==4.21.0" \
    "requests==2.31.0" \
    "flask==3.0.3" \
    "colorama==0.4.6" \
    "urllib3==1.26.18"

echo "=== Starting (auto-restart loop) ==="
while true; do
    python3 netflix_checker.py
    echo "[RESTART] Exited. Restarting in 5s..."
    sleep 5
done
