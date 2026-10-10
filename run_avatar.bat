@echo off
REM Start the Shiv avatar. Use run_avatar.bat debug to keep a console with logs.
cd /d "%~dp0"
if /i "%1"=="debug" (
    avatar_env\Scripts\python.exe shiv_avatar.py
    pause
) else (
    start "" avatar_env\Scripts\pythonw.exe shiv_avatar.py %*
)
