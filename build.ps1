# Build the downloadable Windows setup program:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
# The result is dist\IPTV-Player-Setup.exe
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } else {
        python -m venv .venv
    }
    & $python -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name "IPTV-Player-Setup" `
    --icon (Join-Path $Root "assets\iptv-player.ico") `
    --add-data "assets;assets" `
    --hidden-import vlc `
    --collect-all PyQt6 `
    (Join-Path $Root "main.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Built $(Join-Path $Root 'dist\IPTV-Player-Setup.exe')"
