# UzRail Bot installer for Windows.
#   irm https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.ps1 | iex
# Installs Python if needed, puts the app in %LOCALAPPDATA%\UzRailBot, adds Start menu and desktop shortcuts,
# and opens the app. Run it again to update. Uninstall:  & ([scriptblock]::Create((irm <same url>))) -Uninstall
param(
    [string]$Source = $env:UZRAILBOT_SOURCE,       # local folder instead of downloading (for testing)
    [string]$InstallDir = "$env:LOCALAPPDATA\UzRailBot",
    [switch]$NoLaunch,
    [switch]$NoShortcuts,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = "myrosama/uzrail-ticket-bot"
$AppDir = Join-Path $InstallDir "app"
$Venv = Join-Path $InstallDir "venv"
$Pyw = Join-Path $Venv "Scripts\pythonw.exe"
$Py = Join-Path $Venv "Scripts\python.exe"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "UzRail Bot.lnk"
$Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "UzRail Bot.lnk"

function Say($text) { Write-Host "  $text" }
function Step($text) { Write-Host ""; Write-Host "> $text" -ForegroundColor Cyan }

function Stop-App {
    # ask a running copy to quit cleanly (it cancels nothing; held seats stay held)
    $keyFile = Join-Path $InstallDir "data\app.key"
    if (Test-Path $keyFile) {
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:47613/api/quit" -TimeoutSec 3 `
                -Headers @{ "X-App-Key" = (Get-Content $keyFile -Raw).Trim() } | Out-Null
            Start-Sleep -Seconds 2
        } catch { }
    }
    # then make sure: the venv launcher and the interpreter it started
    $procs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)
    $ours = @($procs | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Venv, [StringComparison]::OrdinalIgnoreCase) })
    $ids = @($ours | ForEach-Object { $_.ProcessId })
    $kids = @($procs | Where-Object { $ids -contains $_.ParentProcessId })
    foreach ($p in $kids + $ours) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Step "Removing UzRail Bot"
    Stop-App
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "UzRailBot" -ErrorAction SilentlyContinue
    Remove-Item $StartMenu, $Desktop -ErrorAction SilentlyContinue
    Remove-Item $AppDir, $Venv -Recurse -Force -ErrorAction SilentlyContinue
    Say "Removed the app. Your settings and logs are still in $InstallDir (delete that folder to remove them too)."
    return
}

Write-Host ""
Write-Host "  UzRail Bot installer" -ForegroundColor Green

# ---------------------------------------------------------------- 1. Python 3.9+
function Test-Python($exe, [string[]]$pre = @()) {
    try {
        $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2]) if sys.version_info >= (3, 9) else None" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^\d+\.\d+$') { return $true }
    } catch { }
    return $false
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-Python "py" @("-3")) { return @{ exe = "py"; pre = @("-3") } }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*" -and (Test-Python $cmd.Source)) { return @{ exe = $cmd.Source; pre = @() } }
    }
    foreach ($dir in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*", "$env:ProgramFiles\Python3*" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending) {
        $exe = Join-Path $dir.FullName "python.exe"
        if ((Test-Path $exe) -and (Test-Python $exe)) { return @{ exe = $exe; pre = @() } }
    }
    return $null
}

Step "Checking Python"
$python = Find-Python
if (-not $python) {
    Say "Python not found; installing Python 3.12 (one time, about a minute)..."
    $done = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
        $done = $LASTEXITCODE -eq 0
    }
    if (-not $done) {
        $setup = Join-Path $env:TEMP "python-3.12-setup.exe"
        $arch = if ([Environment]::Is64BitOperatingSystem) { "-amd64" } else { "" }
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8$arch.exe" -OutFile $setup
        Start-Process $setup -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0" -Wait
        Remove-Item $setup -ErrorAction SilentlyContinue
    }
    $python = Find-Python
    if (-not $python) { throw "Python could not be installed automatically. Install it from https://www.python.org/downloads/ and run this again." }
}
Say ("Using " + (& $python.exe @($python.pre) -c "import sys; print('Python', sys.version.split()[0])"))

# ---------------------------------------------------------------- 2. app files
Step "Getting UzRail Bot"
New-Item -ItemType Directory -Force $InstallDir | Out-Null
Stop-App
$staging = Join-Path $env:TEMP ("uzrailbot-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $staging | Out-Null
try {
    if ($Source) {
        $src = (Resolve-Path $Source).Path
        Say "From local folder $src"
    } else {
        $zip = Join-Path $staging "app.zip"
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip
        Expand-Archive $zip -DestinationPath $staging
        $src = (Get-ChildItem $staging -Directory | Select-Object -First 1).FullName
        Say "Downloaded the latest version from github.com/$Repo"
    }
    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
    New-Item -ItemType Directory -Force $AppDir | Out-Null
    foreach ($item in @("uzrailbot", "pyproject.toml", "requirements.txt", "README.md", "LICENSE")) {
        $p = Join-Path $src $item
        if (Test-Path $p) { Copy-Item $p -Destination $AppDir -Recurse -Force }
    }
    Get-ChildItem $AppDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 3. private Python environment
Step "Installing (this takes a minute the first time)"
if (-not (Test-Path $Py)) {
    & $python.exe @($python.pre) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment in $Venv" }
}
& $Py -m pip install --quiet --disable-pip-version-check --upgrade pip
& $Py -m pip install --quiet --disable-pip-version-check --upgrade $AppDir
if ($LASTEXITCODE -ne 0) { throw "Installing the app's packages failed (see the messages above)." }
Say "Installed version $(& $Py -c 'import uzrailbot; print(uzrailbot.__version__)')"

# ---------------------------------------------------------------- 4. shortcuts
if (-not $NoShortcuts) {
    Step "Adding shortcuts"
    $icon = Join-Path $AppDir "uzrailbot\ui\icon.ico"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($lnk in @($StartMenu, $Desktop)) {
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath = $Pyw
        $s.Arguments = "-m uzrailbot"
        $s.WorkingDirectory = $InstallDir
        $s.IconLocation = $icon
        $s.Description = "UzRail Bot - books train tickets the moment they appear"
        $s.Save()
    }
    Say "Start menu and desktop: UzRail Bot"
}

# ---------------------------------------------------------------- 5. open it
Write-Host ""
Write-Host "  Done!" -ForegroundColor Green
if (-not $NoLaunch) {
    Start-Process -FilePath $Pyw -ArgumentList "-m", "uzrailbot" -WorkingDirectory $InstallDir
    Say "UzRail Bot is opening. Paste your Telegram bot token there to finish setup."
} else {
    Say "Start it from the Start menu: UzRail Bot"
}
Write-Host ""
