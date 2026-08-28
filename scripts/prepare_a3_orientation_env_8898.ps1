param(
    [string]$BasePython = ""
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ShadowRoot = Join-Path $ProjectDir ".shadow_8898"
$VenvDir = Join-Path $ShadowRoot "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectDir "requirements-a3-orientation-shadow.txt"

if (-not $BasePython) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Base Python was not found."
    }
    $BasePython = $pythonCommand.Source
}
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) {
    throw "Base Python does not exist: $BasePython"
}
if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf)) {
    throw "Shadow OCR requirements are missing: $Requirements"
}

New-Item -ItemType Directory -Force -Path $ShadowRoot | Out-Null
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $BasePython -m venv --system-site-packages $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the isolated 8898 OCR environment."
    }
}

& $VenvPython -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install the isolated 8898 OCR dependencies."
}
& $VenvPython -c "import fastapi, rapidocr, onnxruntime"
if ($LASTEXITCODE -ne 0) {
    throw "The isolated 8898 OCR environment failed its import check."
}

Write-Output "PYTHON=$VenvPython"
Write-Output "REQUIREMENTS=$Requirements"
