@echo off
setlocal
cd /d "%~dp0"

set "GUARDIAN_EXE=%~dp0dist\UESTCNetGuardian\UESTCNetGuardian.exe"
if exist "%GUARDIAN_EXE%" (
    "%GUARDIAN_EXE%"
    exit /b %errorlevel%
)

if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment is missing.
    exit /b 1
)
".venv\Scripts\python.exe" "guardian_app.py" %*
exit /b %errorlevel%
