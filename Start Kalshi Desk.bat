@echo off
title Kalshi Claude Desk
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add to PATH". & pause & exit /b 1)
python -c "import flask, requests" 2>nul || (echo Installing requirements, one time only... & python -m pip install -r requirements.txt)
echo.
echo Kalshi Claude Desk is starting. Your browser will open at http://127.0.0.1:8050
echo Keep this window open while you use it. Close it to stop.
echo.
python bot.py
pause
