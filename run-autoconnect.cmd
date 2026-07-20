@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment is missing.
    exit /b 1
)

".venv\Scripts\python.exe" "uestc_srun_autoconnect.py" %*
exit /b %errorlevel%
