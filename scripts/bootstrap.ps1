Param(
    [switch]$Recreate,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$venvDir = Join-Path $repoRoot "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if ($Recreate -and (Test-Path $venvDir)) {
    Write-Host "[bootstrap] Removing existing venv..."
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvPython)) {
    Write-Host "[bootstrap] Creating virtual environment..."
    try {
        & py -3 -m venv $venvDir
    }
    catch {
        & python -m venv $venvDir
    }
}

Write-Host "[bootstrap] Upgrading pip..."
& $venvPython -m pip install --upgrade pip

Write-Host "[bootstrap] Installing requirements..."
& $venvPython -m pip install -r requirements.txt

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[bootstrap] Created .env from .env.example"
}

if (-not $SkipTests) {
    Write-Host "[bootstrap] Running test suite..."
    & $venvPython -m pytest tests -q
}

Write-Host "[bootstrap] Ready. Use: .\venv\Scripts\python.exe -m generator.main --manifest trip_manifest.yaml --output output/"
