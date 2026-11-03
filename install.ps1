# Clone the repository, then run this once:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# It installs the Python packages, 64-bit VLC when it is missing, and a desktop shortcut.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PythonArguments)
    if (Test-Command "py") {
        & py -3 @PythonArguments
    } else {
        & python @PythonArguments
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "Installing IPTV Player..."

if (-not (Test-Command "py") -and -not (Test-Command "python")) {
    Write-Host "Python 3 was not found. Install 64-bit Python from https://www.python.org/downloads/ and tick Add python.exe to PATH."
    exit 1
}

$bits = ""
if (Test-Command "py") {
    $bits = (& py -3 -c "import struct; print(struct.calcsize('P')*8)").Trim()
} else {
    $bits = (& python -c "import struct; print(struct.calcsize('P')*8)").Trim()
}
if ($bits -ne "64") {
    Write-Host "This app needs 64-bit Python so it can load 64-bit VLC. The Python that was found is $bits-bit."
    exit 1
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$venvPythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating a virtual environment..."
    Invoke-Python -m venv .venv
}

Write-Host "Installing Python packages..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPython -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$vlcDll = "C:\Program Files\VideoLAN\VLC\libvlc.dll"
if (-not (Test-Path $vlcDll)) {
    Write-Host "64-bit VLC was not found. Trying to install it..."
    if (Test-Command "winget") {
        winget install --id VideoLAN.VLC -e --accept-package-agreements --accept-source-agreements
    }
    if (-not (Test-Path $vlcDll)) {
        Write-Host "VLC is still missing. Install the 64-bit player from https://www.videolan.org/vlc/ before opening a stream."
    }
}

$icon = Join-Path $Root "assets\iptv-player.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "IPTV Player.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $venvPythonw
$shortcut.Arguments = "`"$(Join-Path $Root 'main.py')`""
$shortcut.WorkingDirectory = $Root
$shortcut.WindowStyle = 1
$shortcut.Description = "IPTV Player"
if (Test-Path $icon) {
    $shortcut.IconLocation = "$icon,0"
}
$shortcut.Save()

Write-Host ""
Write-Host "Done. IPTV Player is on your desktop."
Write-Host "Open that shortcut, then sign in with your Xtream Codes server."
