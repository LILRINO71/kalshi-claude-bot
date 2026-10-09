@echo off
title AI Day Trader (paper)
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add to PATH". & pause & exit /b 1)
python -c "import flask, requests, tzdata" 2>nul || (echo Installing requirements, one time only... & python -m pip install -r requirements.txt)
echo.
echo AI Day Trader - paper trading only, Claude is the brain (log in once with: claude, then /login)
echo.
echo   1. Morning brief: what would it trade today?
echo   2. Live paper trading (leave this window open)
echo   3. Backtest the last ~40 trading days with Claude (learns as it goes; ~7 Claude calls a day, a few hours)
echo   4. Backtest the free no-AI control
echo   5. List backtest runs
echo.
set /p choice="Choose 1-5: "
if "%choice%"=="1" python -m daytrader premarket
if "%choice%"=="2" python -m daytrader live
if "%choice%"=="3" python -m daytrader backtest --save-profile live
if "%choice%"=="4" python -m daytrader backtest --brain gap_and_go
if "%choice%"=="5" python -m daytrader runs
pause
