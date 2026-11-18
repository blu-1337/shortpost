# ShortPost installer: uv + Python 3.12 + deps, checks ffmpeg and Ollama models.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Ensure-Tool($cmd, $wingetId) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { Write-Host "OK  $cmd"; return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "$cmd missing and winget not available" }
    Write-Host "Installing $wingetId ..."
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
}

Ensure-Tool "uv" "astral-sh.uv"
Ensure-Tool "ffmpeg" "Gyan.FFmpeg"
Ensure-Tool "ollama" "Ollama.Ollama"

$env:UV_PYTHON_INSTALL_DIR = Join-Path $repo ".python"
# uv writes progress to stderr, which Windows PowerShell 5.1 treats as an error under "Stop".
$ErrorActionPreference = "Continue"
uv python install 3.12
if ($LASTEXITCODE -ne 0) { throw "uv python install failed" }
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue)
if ($ollama) {
    $have = (ollama list) -join "`n"
    foreach ($m in @("gemma4:12b", "qwen3.5:9b")) {
        if ($have -notmatch [regex]::Escape($m)) { Write-Host "Pulling $m ..."; ollama pull $m }
    }
}

Write-Host "`nDone. Next: uv run shortpost doctor"
