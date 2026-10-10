@echo off
REM One-time setup for the Shiv avatar. Double-click or run from D:\Shiv.
cd /d "%~dp0"

if not exist avatar_env\Scripts\python.exe (
    echo Creating virtual environment avatar_env ...
    py -3.12 -m venv avatar_env 2>nul || python -m venv avatar_env
)

echo Installing packages (PyQt6 WebEngine and faster-whisper are large; this takes a few minutes) ...
avatar_env\Scripts\python.exe -m pip install --upgrade pip
avatar_env\Scripts\python.exe -m pip install -r requirements-avatar.txt
if errorlevel 1 (
    echo.
    echo Package install failed - scroll up for the error.
    pause
    exit /b 1
)

if not exist .env (
    copy .env.example .env >nul
    echo Created .env - open it and paste your ANTHROPIC_API_KEY ^(and LiveAvatar key if you have one^).
    notepad .env
)

echo.
echo Running self-tests ...
avatar_env\Scripts\python.exe -m pip install -q pytest pytest-asyncio
avatar_env\Scripts\python.exe -m pytest tests -q

echo.
echo Done. Start Shiv with run_avatar.bat
pause
