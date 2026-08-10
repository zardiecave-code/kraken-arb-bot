@echo off
REM Runs the bot in whatever mode .env specifies. Default is scan-only.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv || exit /b 1
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1
)
if exist "STOP" (
  echo Kill switch STOP is present. Delete it to allow trading.
  exit /b 1
)
.venv\Scripts\python.exe main.py
