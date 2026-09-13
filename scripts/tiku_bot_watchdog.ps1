param(
    [int]$Port = 8788,
    [string]$TunnelName = $env:TIKU_TUNNEL_NAME,
    [string]$CloudflaredConfig = $env:TIKU_CLOUDFLARED_CONFIG,
    [string]$PublicHost = $env:TIKU_PUBLIC_HOST,
    [int]$MaxMessageAgeMinutes = 15,
    [string]$RuntimeDir,
    [string]$PythonExe = 'python',
    [string]$AdminFeeDatabase,
    [switch]$ExternalTunnel,
    [switch]$EnableStoreTextOrientation,
    [switch]$EnrollAdminSenderOnce,
    [switch]$FunctionsOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'watchdog_process_guard.ps1')
. (Join-Path $PSScriptRoot 'tiku_agent_watchdog_8790_safety.ps1')

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BotEntrypoint = Join-Path $ProjectDir 'scripts\feishu_tiku_bot.py'
function Test-BotAbsolutePath([string]$Value) { return $Value -match '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/])' }
if ($RuntimeDir -and -not (Test-BotAbsolutePath $RuntimeDir)) { throw 'RuntimeDir must be absolute.' }
if ($AdminFeeDatabase -and (-not (Test-BotAbsolutePath $AdminFeeDatabase) -or
    -not (Test-Path -LiteralPath $AdminFeeDatabase -PathType Leaf))) { throw 'AdminFeeDatabase must be an existing absolute file.' }
$LogDir = if ($RuntimeDir) { [IO.Path]::GetFullPath($RuntimeDir) } else { Join-Path $ProjectDir '.tmp_feishu_tiku' }
$ExpectedPythonPath = Resolve-WatchdogExecutablePath -Executable $PythonExe
if ($env:TIKU_BANK_STORE) {
    if (-not $RuntimeDir -or -not (Test-BotAbsolutePath $PythonExe) -or -not $AdminFeeDatabase) {
        throw 'Managed Feishu requires explicit runtime, Python executable and fee database.'
    }
    if (-not $env:TIKU_CONFIG_FILE -or -not (Test-BotAbsolutePath $env:TIKU_CONFIG_FILE) -or
        -not (Test-Path -LiteralPath $env:TIKU_CONFIG_FILE -PathType Leaf)) {
        throw 'Managed Feishu requires an explicit external service configuration.'
    }
    foreach ($root in @($ProjectDir, [IO.Path]::GetFullPath($env:TIKU_BANK_STORE))) {
        if ($LogDir.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
            $LogDir.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $root.StartsWith($LogDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Managed Feishu runtime must be separate from source and published bank.'
        }
    }
    $cacheRoot = if ($env:TIKU_SEARCH_STATE_DIR) { $env:TIKU_SEARCH_STATE_DIR } else { Join-Path $LogDir 'search-cache' }
    if (-not (Test-BotAbsolutePath $cacheRoot) -or
        -not [IO.Path]::GetFullPath($cacheRoot).StartsWith($LogDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Managed Feishu search cache must stay within its runtime directory.'
    }
    $env:TIKU_SEARCH_STATE_DIR = [IO.Path]::GetFullPath($cacheRoot)
}
$StatusFile = Join-Path $LogDir "tiku_bot_status.txt"
$BotOutLog = Join-Path $LogDir "tiku_bot.out.log"
$BotErrLog = Join-Path $LogDir "tiku_bot.err.log"
$TunnelOutLog = Join-Path $LogDir "cloudflared.out.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared.err.log"
$BotPidFile = Join-Path $LogDir "tiku_bot.pid"
$TunnelPidFile = Join-Path $LogDir "cloudflared.pid"
$UrlFile = Join-Path $LogDir "feishu_tiku_latest_url.txt"

if ($ExternalTunnel -and -not $PublicHost) {
    throw "ExternalTunnel requires PublicHost."
}

function Write-Status {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $StatusFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-Health {
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
            if ([bool]$response.ok) { return $true }
        } catch {
            if ($attempt -lt 2) { Start-Sleep -Milliseconds 400 }
        }
    }
    return $false
}

function Stop-PortProcess {
    $processIds = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $processIds) {
        if ($processId -and $processId -ne 0) {
            Write-Status "Stopping stale tiku bot listener on port ${Port}: PID $processId"
            Stop-Process -Id $processId -Force -ErrorAction Stop
        }
    }
}

function Wait-PortFree {
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Port $Port did not become free before restart."
}

function Wait-BotHealthy {
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        if (Test-Health) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-BotArguments {
    $arguments = @(
        '-B', $BotEntrypoint,
        "--port", "$Port",
        "--max-message-age-minutes", "$MaxMessageAgeMinutes",
        '--temp-dir', $LogDir
    )
    if ($AdminFeeDatabase) { $arguments += @('--admin-fee-db', [IO.Path]::GetFullPath($AdminFeeDatabase)) }
    if ($EnrollAdminSenderOnce) {
        $arguments += "--enroll-admin-sender-once"
    }
    if ($EnableStoreTextOrientation) {
        $arguments += "--enable-store-text-orientation"
    }
    return $arguments
}

function Start-Bot {
    $arguments = @(Get-BotArguments | ForEach-Object { ConvertTo-Tiku8790CommandLineArgument -Argument $_ })
    $process = Start-Process -FilePath $ExpectedPythonPath `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $BotOutLog `
        -RedirectStandardError $BotErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $BotPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started tiku bot: PID $($process.Id)"
    return $process
}

function Start-Tunnel {
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Status "cloudflared not found in PATH; cannot start temporary tunnel."
        return $null
    }

    if ($TunnelName -or $CloudflaredConfig -or $PublicHost) {
        $arguments = @("tunnel")
        if ($CloudflaredConfig) {
            $arguments += @("--config", $CloudflaredConfig)
        }
        if ($TunnelName) {
            $arguments += @("run", $TunnelName)
        } else {
            $arguments += @("--url", "http://127.0.0.1:$Port")
        }
        $mode = "configured"
    } else {
        $arguments = @("tunnel", "--url", "http://127.0.0.1:$Port")
        $mode = "temporary trycloudflare"
    }

    foreach ($path in @($TunnelOutLog, $TunnelErrLog)) {
        if (Test-Path -LiteralPath $path) {
            Clear-Content -LiteralPath $path
        }
    }
    $process = Start-Process cloudflared `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $TunnelOutLog `
        -RedirectStandardError $TunnelErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $TunnelPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started cloudflared ($mode): PID $($process.Id)"
    return $process
}

function Get-TunnelUrl {
    if ($PublicHost) {
        return "https://$PublicHost/feishu/events"
    }
    $content = ""
    foreach ($path in @($TunnelOutLog, $TunnelErrLog)) {
        if (Test-Path -LiteralPath $path) {
            $content += "`n" + (Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
        }
    }
    $match = [regex]::Match($content, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    if ($match.Success) {
        return "$($match.Value)/feishu/events"
    }
    return $null
}

if ($FunctionsOnly) { return }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port PublicHost=$PublicHost ExternalTunnel=$ExternalTunnel MaxMessageAgeMinutes=$MaxMessageAgeMinutes" -Encoding UTF8
foreach ($path in @($BotOutLog, $BotErrLog, $TunnelOutLog, $TunnelErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
}

$botProcess = $null
$tunnelProcess = $null
$lastUrl = ""

while ($true) {
    if (-not $botProcess -or $botProcess.HasExited -or -not (Test-Health)) {
        if ($botProcess -and -not $botProcess.HasExited) {
            Stop-Process -Id $botProcess.Id -Force -ErrorAction Stop
        }
        Stop-PortProcess
        Wait-PortFree
        Write-Status "Bot health check failed; restarting bot."
        $botProcess = Start-Bot
        if (Wait-BotHealthy) {
            Write-Status "Bot health check passed."
        } else {
            Write-Status "Bot failed to become healthy after startup; stopping it."
            if ($botProcess -and -not $botProcess.HasExited) {
                Stop-Process -Id $botProcess.Id -Force -ErrorAction Stop
                Wait-Process -Id $botProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
            }
            Stop-PortProcess
            Wait-PortFree
        }
    }

    if (-not $ExternalTunnel -and (-not $tunnelProcess -or $tunnelProcess.HasExited)) {
        Write-Status "Tunnel process is not running; restarting tunnel."
        $tunnelProcess = Start-Tunnel
    }

    $url = Get-TunnelUrl
    if ($url -and $url -ne $lastUrl) {
        $lastUrl = $url
        Set-Content -LiteralPath $UrlFile -Value $url -Encoding UTF8
        Write-Status "Feishu event URL: $url"
    }

    Start-Sleep -Seconds 20
}
