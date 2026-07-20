$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $project

& ".venv\Scripts\python.exe" "tools\generate_guardian_icon.py"
& ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean "UESTCNetGuardian.spec"

$release = Join-Path $project "dist\UESTCNetGuardian"
Write-Host "Built: $release\UESTCNetGuardian.exe"
