<#
.SYNOPSIS
    Richtet SchulSync in diesem Ordner ein (Einsteiger-Skript).

.DESCRIPTION
    Was das Skript tut:
      1. sucht Python 3.11 oder neuer,
      2. legt im Repository-Ordner die virtuelle Umgebung ".venv" an (falls sie fehlt),
      3. installiert SchulSync mit "pip install -e ." in diese Umgebung,
      4. zeigt die nächsten Befehle an.

    Was das Skript NICHT tut: Es braucht keine Administratorrechte, ändert keine Windows-Einstellungen
    (auch nicht die Ausführungsrichtlinie von PowerShell), richtet keinen Autostart ein und fragt keine Passwörter ab.
    Den Autostart richtest du bewusst selbst mit "schulsync autostart install" ein.

    Aufruf (im Repository-Ordner):
        powershell -File scripts\install.ps1

    Falls Windows meldet, dass die Skriptausführung deaktiviert ist, starte das Skript einmalig so
    (gilt nur für diesen einen Aufruf, nichts wird dauerhaft verändert):
        powershell -ExecutionPolicy Bypass -File scripts\install.ps1

    Das Skript kann gefahrlos mehrfach ausgeführt werden.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "FEHLER: $Message" -ForegroundColor Red
    exit 1
}

# Repository-Wurzel = Ordner über "scripts"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
    Fail "In '$RepoRoot' wurde keine pyproject.toml gefunden. Bitte das Skript aus dem SchulSync-Ordner starten: scripts\install.ps1"
}

$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

function Find-Python {
    # "py -3" (Python-Launcher) zuerst, dann "python". Der Windows-Store-Platzhalter liefert einen Fehlercode und wird übersprungen.
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { continue }
        $out = $null
        try {
            $out = & $cmd.Source @($c.Args) -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))"
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $version = $null
        if ([version]::TryParse(([string]$out).Trim(), [ref]$version) -and $version -ge [version]'3.11') {
            return @{ Exe = $cmd.Source; Args = $c.Args; Version = $version }
        }
    }
    return $null
}

Step 'Python suchen'
$python = Find-Python
if (-not $python) {
    Fail ("Python 3.11 oder neuer wurde nicht gefunden. Bitte von https://www.python.org/downloads/ installieren " +
          "(beim Setup 'Add python.exe to PATH' ankreuzen) und das Skript danach erneut starten.")
}
Write-Host "Gefunden: Python $($python.Version) ($($python.Exe))"

Step 'Virtuelle Umgebung (.venv)'
if (Test-Path -LiteralPath $VenvPython) {
    Write-Host 'Die Umgebung .venv ist schon vorhanden und wird weiterverwendet.'
} else {
    & $python.Exe @($python.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Fail "Die virtuelle Umgebung konnte nicht angelegt werden (Ordner: $VenvDir)."
    }
    Write-Host 'Die Umgebung .venv wurde angelegt.'
}

Step 'SchulSync installieren (pip install -e .)'
& $VenvPython -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Fail "Die Installation ist fehlgeschlagen. Bitte die Meldungen oben prüfen (Internetverbindung?) und das Skript erneut starten."
}

Write-Host ''
Write-Host 'Fertig. SchulSync ist installiert.' -ForegroundColor Green
Write-Host ''
Write-Host "Nächste Schritte (in PowerShell, im Ordner $RepoRoot):"
Write-Host '  1. Einrichten (Zugangsdaten, Google-Anmeldung):   .\.venv\Scripts\schulsync setup'
Write-Host '  2. Verbindungen prüfen:                           .\.venv\Scripts\schulsync check'
Write-Host '  3. Autostart nach der Windows-Anmeldung einrichten: .\.venv\Scripts\schulsync autostart install'
Write-Host ''
Write-Host 'Hinweise: Administratorrechte sind nicht nötig. Den Autostart kannst du jederzeit mit'
Write-Host '".\.venv\Scripts\schulsync autostart remove" wieder entfernen.'
