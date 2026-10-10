@echo off
title AI Day Trader (paper)
cd /d "%~dp0"
rem Find a real Python: "python" can be the Microsoft Store placeholder, so test that it runs.
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (py -3 -c "import sys" >nul 2>nul && set "PY=py -3")
if not defined PY (echo Python 3.10+ is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
%PY% -c "import flask, requests, tzdata" 2>nul || (echo Installing requirements, one time only... & %PY% -m pip install -r requirements.txt)
echo.
echo AI Day Trader - paper trading only, Claude is the brain (log in once with: claude, then /login)
echo.
echo   1. Open the dashboard (backtests, comparisons, live account, usage, settings)
echo   2. Live paper trading (leave this window open)
echo   3. Morning brief: what would it trade today?
echo   4. Backtest the last ~40 trading days with Claude (learns as it goes; ~7 Claude calls a day, about an hour with Sonnet)
echo   5. Backtest the free no-AI control
echo   6. List backtest runs
echo.
set /p choice="Choose 1-6: "
if "%choice%"=="1" %PY% -m daytrader dashboard
if "%choice%"=="2" %PY% -m daytrader live
if "%choice%"=="3" %PY% -m daytrader premarket
if "%choice%"=="4" %PY% -m daytrader backtest --save-profile live
if "%choice%"=="5" %PY% -m daytrader backtest --brain gap_and_go
if "%choice%"=="6" %PY% -m daytrader runs
pause
