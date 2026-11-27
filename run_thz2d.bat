@echo off
REM Launch the 2D THz acquisition GUI on the lab PC (uses .venv if present).
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m thz2d %*
) else (
    python -m thz2d %*
)
if errorlevel 1 pause
