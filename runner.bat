@echo off
title Netflix Cookie Checker Auto-Runner
color 0A

echo [1] Installing dependencies (playwright, requests, colorama, pyTelegramBotAPI)...
pip install requests colorama pyTelegramBotAPI playwright
echo [1.1] Installing Browser Binaries...
playwright install chromium

echo.
echo [2] Starting Checker...
python netflix_checker.py

pause