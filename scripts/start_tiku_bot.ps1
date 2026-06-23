param(
    [int]$Port = 8788,
    [string]$TunnelName = $env:TIKU_TUNNEL_NAME,
    [string]$CloudflaredConfig = $env:TIKU_CLOUDFLARED_CONFIG,
    [string]$PublicHost = $env:TIKU_PUBLIC_HOST,
    [int]$MaxMessageAgeMinutes = 15,
    [switch]$NoMonitorWindows
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $ProjectDir ".tmp_feishu_tiku"
$WatchdogScript = Join-Path $PSScriptRoot "tiku_bot_watchdog.ps1"
$WatchdogPidFile = Join-Path $LogDir "tiku_bot_watchdog.pid"
$BotPidFile = Join-Path $LogDir "tiku_bot.pid"
$TunnelPidFile = Join-Path $LogDir "cloudflared.pid"
$StatusFile = Join-Path $LogDir "tiku_bot_status.txt"
$UrlFile = Join-Path $LogDir "feishu_tiku_latest_url.txt"
$BotOutLog = Join-Path $LogDir "tiku_bot.out.log"
$BotErrLog = Join-Path $LogDir "tiku_bot.err.log"
$TunnelOutLog = Join-Path $LogDir "cloudflared.out.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared.err.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Stop-PidFile {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $pidText = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    $processId = 0
    if ([int]::TryParse(($pidText -as [string]).Trim(), [ref]$processId)) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Write-Host "Stopping old ${Label}: PID $processId"
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $Path -ErrorAction SilentlyContinue
}

function Stop-PortProcess {
    $processIds = @()
    $processIds += Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -ExpandProperty OwningProcess -Unique

    $netstatPattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $processIds += netstat -ano |
        ForEach-Object {
            $match = [regex]::Match($_, $netstatPattern)
            if ($match.Success) { [int]$match.Groups[1].Value }
        }

    $processIds = $processIds | Where-Object { $_ -and $_ -ne 0 } | Sort-Object -Unique
    foreach ($processId in $processIds) {
        Write-Host "Stopping old tiku bot listener on port ${Port}: PID $processId"
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "Missing watchdog script: $WatchdogScript"
}

Stop-PidFile -Path $WatchdogPidFile -Label "tiku watchdog"
Stop-PidFile -Path $BotPidFile -Label "tiku bot"
Stop-PidFile -Path $TunnelPidFile -Label "tiku cloudflared"
Stop-PortProcess

foreach ($path in @($StatusFile, $UrlFile, $BotOutLog, $BotErrLog, $TunnelOutLog, $TunnelErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType File -Path $path -Force | Out-Null
    }
}

$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $WatchdogScript,
    "-Port", $Port,
    "-MaxMessageAgeMinutes", $MaxMessageAgeMinutes
)
if ($TunnelName) {
    $arguments += @("-TunnelName", $TunnelName)
}
if ($CloudflaredConfig) {
    $arguments += @("-CloudflaredConfig", $CloudflaredConfig)
}
if ($PublicHost) {
    $arguments += @("-PublicHost", $PublicHost)
}

$watchdog = Start-Process powershell.exe `
    -ArgumentList $arguments `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $WatchdogPidFile -Value $watchdog.Id -Encoding ASCII

if (-not $NoMonitorWindows) {
    $monitors = @(
        @{ Title = "Tiku Bot Status"; Path = $StatusFile }
    )
    foreach ($monitor in $monitors) {
        $title = $monitor.Title
        $path = $monitor.Path
        $command = @"
`$Host.UI.RawUI.WindowTitle = '$title'
Write-Host '$title'
Write-Host '$path'
Write-Host ''
Write-Host 'This is a monitor window. Closing it will not stop the tiku bot.'
Write-Host ''
Get-Content -LiteralPath '$path' -Wait -Tail 40 -Encoding UTF8
"@
        Start-Process powershell.exe -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-Command", $command
        ) -WindowStyle Normal | Out-Null
    }
}

Write-Host "Tiku bot watchdog started: PID $($watchdog.Id)"
Write-Host "Status file: $StatusFile"
Write-Host "Latest Feishu URL file: $UrlFile"
Write-Host ""
if ($PublicHost) {
    Write-Host "Feishu event URL should stay fixed:"
    Write-Host "https://$PublicHost/feishu/events"
} else {
    Write-Host "Temporary tunnel fallback is enabled."
    Write-Host "When the status window prints a trycloudflare URL, paste it into Feishu event subscription."
}
Write-Host ""
Write-Host "You can close this window. The tiku bot keeps running in the background."
Start-Sleep -Seconds 5
