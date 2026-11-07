@echo off
title UzRail Ticket Bot
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Installing it with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  echo Close this window and double-click start.bat again.
  pause
  exit /b
)
if not exist .venv\Scripts\python.exe (
  echo First run: preparing the bot, this takes a minute...
  python -m venv .venv
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt
)
.venv\Scripts\python -m uzrailbot %*
pause
