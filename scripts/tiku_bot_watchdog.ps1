param(
    [int]$Port = 8788,
    [string]$TunnelName = $env:TIKU_TUNNEL_NAME,
    [string]$CloudflaredConfig = $env:TIKU_CLOUDFLARED_CONFIG,
    [string]$PublicHost = $env:TIKU_PUBLIC_HOST,
    [int]$MaxMessageAgeMinutes = 15
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $ProjectDir ".tmp_feishu_tiku"
$StatusFile = Join-Path $LogDir "tiku_bot_status.txt"
$BotOutLog = Join-Path $LogDir "tiku_bot.out.log"
$BotErrLog = Join-Path $LogDir "tiku_bot.err.log"
$TunnelOutLog = Join-Path $LogDir "cloudflared.out.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared.err.log"
$BotPidFile = Join-Path $LogDir "tiku_bot.pid"
$TunnelPidFile = Join-Path $LogDir "cloudflared.pid"
$UrlFile = Join-Path $LogDir "feishu_tiku_latest_url.txt"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Status {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $StatusFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return [bool]$response.ok
    } catch {
        return $false
    }
}

function Start-Bot {
    $arguments = @(
        "scripts\feishu_tiku_bot.py",
        "--port", "$Port",
        "--max-message-age-minutes", "$MaxMessageAgeMinutes"
    )
    $process = Start-Process python `
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

Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port PublicHost=$PublicHost MaxMessageAgeMinutes=$MaxMessageAgeMinutes" -Encoding UTF8
foreach ($path in @($BotOutLog, $BotErrLog, $TunnelOutLog, $TunnelErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
}

$botProcess = $null
$tunnelProcess = $null
$lastUrl = ""

while ($true) {
    if (-not $botProcess -or $botProcess.HasExited -or -not (Test-Health)) {
        if ($botProcess -and -not $botProcess.HasExited) {
            Stop-Process -Id $botProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Status "Bot health check failed; restarting bot."
        $botProcess = Start-Bot
        Start-Sleep -Seconds 4
        if (Test-Health) {
            Write-Status "Bot health check passed."
        }
    }

    if (-not $tunnelProcess -or $tunnelProcess.HasExited) {
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
