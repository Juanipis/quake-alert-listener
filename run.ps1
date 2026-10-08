# ==============================================================================
# Quake MCS Listener - Zero-Install One-Line Bridge for Windows (PowerShell)
# Runs the lightweight earthquake listener without manual Python installation
# Built with Google Antigravity (AGY) & Gemini 3.8 Flash (Thinking High)
# ==============================================================================

param(
    [string]$Lat = "",
    [string]$Lon = "",
    [string]$Name = "",
    [int]$PingInterval = 120,
    [int]$HttpPort = 8990
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "  🌍 Quake MCS Listener • Real-Time Earthquake Bridge (Windows)" -ForegroundColor Cyan
Write-Host "  Early warnings, quake feeds and your own sensor -> Home Assistant" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host ""

$ScriptUrl = "https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py"
$TempDir = Join-Path $env:TEMP "quake-bridge"
if (-not (Test-Path $TempDir)) {
    New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
}

# 1. Location Detection
if ([string]::IsNullOrWhiteSpace($Lat) -or [string]::IsNullOrWhiteSpace($Lon)) {
    Write-Host "🔍 Auto-detecting your approximate location via IP..." -ForegroundColor Gray
    try {
        $ipInfo = Invoke-RestMethod -Uri "https://ipapi.co/json/" -TimeoutSec 5 -ErrorAction SilentlyContinue
        if ($ipInfo -and $ipInfo.latitude -and $ipInfo.longitude) {
            $Lat = $ipInfo.latitude.ToString()
            $Lon = $ipInfo.longitude.ToString()
            $Name = "$($ipInfo.city), $($ipInfo.country_name)"
            Write-Host "📍 Detected location: $Name ($Lat, $Lon)" -ForegroundColor Green
        }
    } catch {}
    # Fall back to the generic default when detection failed OR returned no coordinates
    if ([string]::IsNullOrWhiteSpace($Lat) -or [string]::IsNullOrWhiteSpace($Lon)) {
        $Lat = "0.0"
        $Lon = "0.0"
        $Name = "Base Station"
        Write-Host "📍 Using default location: $Name ($Lat, $Lon)" -ForegroundColor Yellow
    }
} else {
    if ([string]::IsNullOrWhiteSpace($Name)) { $Name = "Local Station" }
    Write-Host "📍 User specified location: $Name ($Lat, $Lon)" -ForegroundColor Green
}

# 2. Check for Python in PATH or download portable standalone Python
$PythonExe = ""
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) {
    try {
        $ver = & python -c "import sys; print(int(sys.version_info >= (3, 8)))" 2>$null
        if ($ver -eq "1") {
            $PythonExe = "python"
            Write-Host "✓ Found existing Python 3.8+ installation in PATH." -ForegroundColor Gray
        }
    } catch {}
}

if (-not $PythonExe) {
    $embedDir = Join-Path $env:TEMP "quake-py-embed"
    $embedExe = Join-Path $embedDir "python.exe"
    if (Test-Path $embedExe) {
        $PythonExe = $embedExe
        Write-Host "✓ Reusing cached zero-install portable Python." -ForegroundColor Gray
    } else {
        Write-Host "📦 Python not found. Downloading official zero-install portable Python (~10MB)..." -ForegroundColor Yellow
        $zipUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
        $zipPath = Join-Path $env:TEMP "python-embed.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        Write-Host "📂 Unpacking portable environment..." -ForegroundColor Gray
        Expand-Archive -Path $zipPath -DestinationPath $embedDir -Force
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        $PythonExe = $embedExe
        Write-Host "✓ Portable Python environment ready (0 registry changes, 0 admin rights needed)." -ForegroundColor Green
    }
}

# 3. Fetch latest quake_listener.py
$ListenerFile = Join-Path $TempDir "quake_listener.py"
Write-Host "⬇️  Fetching latest quake_listener.py..." -ForegroundColor Gray
Invoke-WebRequest -Uri $ScriptUrl -OutFile $ListenerFile -UseBasicParsing

Write-Host ""
Write-Host "🚀 Starting Quake MCS Bridge on http://127.0.0.1:$HttpPort ..." -ForegroundColor Cyan
Write-Host "🌐 Opening web app in your browser: https://juanipis.github.io/quake-alert-listener/" -ForegroundColor Green
Write-Host "   (The web app will automatically detect this bridge and connect in real-time)" -ForegroundColor Gray
Write-Host "⌨️  Press Ctrl+C at any time to stop." -ForegroundColor Gray
Write-Host ""

Start-Process "https://juanipis.github.io/quake-alert-listener/"

& $PythonExe $ListenerFile --lat $Lat --lon $Lon --name $Name --ping-interval $PingInterval --http-port $HttpPort
