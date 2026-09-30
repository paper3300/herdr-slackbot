# Plugin hook / action entry point: runs `python -m herdr_slackbot <command>` with the plugin venv.
#   launch   (startup hook) create workspace herdr-slack if needed and start the bridge in a fresh tab
#   restart  stop the running bridge (addressed stop request) and start it again in a fresh tab
#   stop     stop the running bridge
#   status   print bridge status (with -Notify: also as a Herdr notification)
#   open-wizard  (setup action) open a focused tab in the current workspace running the setup wizard
# The venv is created on first use when missing (e.g. after `herdr plugin link`).
param(
    [Parameter(Position = 0)][ValidateSet("launch", "restart", "stop", "status", "open-wizard")][string]$Command = "status",
    [switch]$Notify
)

$ErrorActionPreference = "Continue"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "herdr-slackbot: venv missing, running scripts\setup.ps1 first"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) {
        Write-Error "herdr-slackbot: setup failed; run scripts\setup.ps1 manually"
        exit 1
    }
}

$pyArgs = @("-m", "herdr_slackbot", $Command)
if ($Notify) { $pyArgs += "--notify" }
Push-Location $root
try {
    & $py @pyArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
