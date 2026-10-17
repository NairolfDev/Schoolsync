<#
.SYNOPSIS
    Baut dist\SchulSync-Setup.exe (+ .sha256) aus dem Quellcode. Aufruf im Projektordner: powershell -File scripts\build_exe.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $py -m pip install --quiet -e ".[build]"
& $py -m PyInstaller --noconfirm --clean --onefile --name schulsync --paths src `
    --add-data "config.example.toml;." --collect-data googleapiclient --collect-all tzdata --collect-submodules schulsync `
    packaging\entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller ist fehlgeschlagen." }
Copy-Item dist\schulsync.exe dist\SchulSync-Setup.exe -Force
$hash = (Get-FileHash dist\SchulSync-Setup.exe -Algorithm SHA256).Hash.ToLower()
"$hash *SchulSync-Setup.exe" | Out-File -Encoding ascii dist\SchulSync-Setup.exe.sha256
Write-Host "Fertig: dist\SchulSync-Setup.exe ($hash)"
