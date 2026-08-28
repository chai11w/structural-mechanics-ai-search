param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SourceDir = Join-Path $ProjectDir ".shadow_8898\src"
$RuntimeDir = Join-Path $ProjectDir ".tmp_tiku_agent_a3_shadow_8898"
$LogDir = Join-Path $RuntimeDir "service_logs"
$PidFile = Join-Path $LogDir "tiku_8898.pid"
$OutLog = Join-Path $LogDir "tiku_8898.out.log"
$ErrLog = Join-Path $LogDir "tiku_8898.err.log"
$Port = 8898

if (-not $PythonExe) {
    $shadowPython = Join-Path $ProjectDir ".shadow_8898\venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $shadowPython -PathType Leaf) {
        $PythonExe = $shadowPython
    } else {
        throw "8898 OCR environment is missing. Run scripts\prepare_a3_orientation_env_8898.ps1 first."
    }
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}
& $PythonExe -c "import fastapi, rapidocr, onnxruntime"
if ($LASTEXITCODE -ne 0) {
    throw "8898 OCR environment is incomplete. Run scripts\prepare_a3_orientation_env_8898.ps1."
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceDir "scripts\run_tiku_agent_8898.py") -PathType Leaf)) {
    throw "8898 source snapshot is missing: $SourceDir"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "Port $Port is already in use by PID $($listener[0].OwningProcess)."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$arguments = @(
    "-B",
    "scripts\run_tiku_agent_8898.py",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--runtime-dir", "$RuntimeDir"
)
$process = Start-Process $PythonExe `
    -ArgumentList $arguments `
    -WorkingDirectory $SourceDir `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII

$healthy = $false
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if (($response.status -eq "ok") -or ($response.ok -eq $true)) {
            $healthy = $true
            break
        }
    } catch {
        if ($process.HasExited) {
            break
        }
    }
}

if (-not $healthy) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    $tail = if (Test-Path -LiteralPath $ErrLog) {
        (Get-Content -LiteralPath $ErrLog -Tail 20) -join [Environment]::NewLine
    } else {
        "No stderr log was created."
    }
    throw "8898 failed its startup health check.$([Environment]::NewLine)$tail"
}

$activeListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (@($activeListener).Count -ne 1) {
    throw "8898 listener identity is ambiguous after startup."
}
$listenerPid = [int]$activeListener[0].OwningProcess
$listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid"
if (-not $listenerProcess -or
    $listenerProcess.CommandLine -notlike "*scripts\run_tiku_agent_8898.py*") {
    throw "8898 listener does not match the expected shadow command."
}
Set-Content -LiteralPath $PidFile -Value $listenerPid -Encoding ASCII

Write-Output "PID=$listenerPid"
Write-Output "LAUNCHER_PID=$($process.Id)"
Write-Output "URL=http://127.0.0.1:$Port"
Write-Output "SOURCE=$SourceDir"
Write-Output "RUNTIME=$RuntimeDir"
