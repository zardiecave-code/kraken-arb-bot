@echo off
rem ---------------------------------------------------------------------------
rem  Builds dist\kraken-arb-bot.exe — a single self-contained binary with
rem  Python and every dependency inside. Needs no Python on the target machine.
rem
rem  The exe reads .env and writes logs\ and state.json NEXT TO ITSELF, so keep
rem  it in its own folder. Without a .env it still runs both scanners, which
rem  need no API keys.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtualenv...
    python -m venv .venv || exit /b 1
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || exit /b 1
)

".venv\Scripts\python.exe" -m pip install --quiet pyinstaller || exit /b 1

echo Building...
".venv\Scripts\python.exe" -m PyInstaller ^
    --onefile ^
    --console ^
    --name kraken-arb-bot ^
    --collect-submodules websockets ^
    --hidden-import scan ^
    --hidden-import scan_cross ^
    --clean ^
    --noconfirm ^
    launcher.py || exit /b 1

echo.
echo Built: %~dp0dist\kraken-arb-bot.exe
echo Copy it somewhere with a .env alongside (or run it bare for scan-only).
