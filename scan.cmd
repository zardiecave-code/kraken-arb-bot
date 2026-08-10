@echo off
REM Read-only reconnaissance. No API keys, no orders. Arg = seconds (default 60).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv || exit /b 1
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe scan.py %1
