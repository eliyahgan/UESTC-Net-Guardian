@echo off
setlocal
cd /d "%~dp0"

set "GUARDIAN_EXE=%~dp0dist\UESTCNetGuardian\UESTCNetGuardian.exe"
if not exist "%GUARDIAN_EXE%" (
    echo UESTCNetGuardian.exe is missing. Build it with build_guardian.ps1 first.
    exit /b 1
)

if /I "%~1"=="remove" (
    "%GUARDIAN_EXE%" --startup disable
) else if /I "%~1"=="status" (
    "%GUARDIAN_EXE%" --startup status
) else (
    "%GUARDIAN_EXE%" --startup enable
)
exit /b %errorlevel%
