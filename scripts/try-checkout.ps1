# Launch this checkout's Desktop, TUI, or CLI in a private Hermes home.
# Nothing here writes the installed profile, tool store, or Desktop app.
# Trial state lives in <checkout>\.try-checkout\.
#
#   powershell -ExecutionPolicy Bypass -File scripts\try-checkout.ps1 desktop
#   powershell -ExecutionPolicy Bypass -File scripts\try-checkout.ps1 tui
#   powershell -ExecutionPolicy Bypass -File scripts\try-checkout.ps1 cli
#
# macOS, Linux, and WSL: scripts/try-checkout.sh
# TRY_CHECKOUT_SKIP_SETUP and HERMES_TRY_LAUNCHER are for tests. Leave them unset.

param(
    [Parameter(Position = 0)]
    [string]$Surface = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

if ($MyInvocation.InvocationName -eq '.') {
    Write-Error 'Run: powershell -ExecutionPolicy Bypass -File scripts\try-checkout.ps1 desktop'
    return
}

$ErrorActionPreference = 'Stop'
if ($null -eq $Rest) { $Rest = @() }

$Repo = Split-Path -Parent $PSScriptRoot
$Sandbox = if ($env:HERMES_TRY_DIR) { $env:HERMES_TRY_DIR } else { Join-Path $Repo '.try-checkout' }

function Show-Usage {
    Write-Host @'
Usage: scripts\try-checkout.ps1 <desktop|tui|cli|build> [options]

Run this checkout's Desktop, TUI, or CLI in a private Hermes home.
The installed Hermes profile, tool store, and Desktop app are left alone.

  desktop            Build if needed, then open Desktop
  desktop --rebuild  Rebuild Desktop, then open it
  tui                Open the terminal UI (later args are passed through)
  cli                Open the classic CLI (later args are passed through)
  build              Build Desktop without opening a window
  exec -- CMD...     Run CMD in the private home (after the trial Python exists)

macOS, Linux, and WSL:
  scripts/try-checkout.sh desktop

The trial is <checkout>\.try-checkout\. Delete that folder to reset it.
Sign in again inside the trial. It keeps its own profile.
The first Desktop or TUI launch installs Python and builds the UI.
'@
}

function Apply-Isolation {
    $env:HERMES_HOME = Join-Path $Sandbox 'home'
    $env:HERMES_RUNTIME_DIR = Join-Path $Sandbox 'tools'
    $env:HERMES_DESKTOP_USER_DATA_DIR = Join-Path $Sandbox 'desktop'
    $env:HERMES_DESKTOP_APP_NAME = 'Hermes Try'
    $env:HERMES_DESKTOP_IGNORE_EXISTING = '1'
    $env:HERMES_DESKTOP_ISOLATED_BACKEND = '1'
    $env:HERMES_DESKTOP_HERMES_ROOT = $Repo
    $env:XDG_DATA_HOME = Join-Path $Sandbox 'xdg'
    $env:UV_CACHE_DIR = Join-Path $Sandbox 'uv-cache'
    $env:NPM_CONFIG_CACHE = Join-Path $Sandbox 'npm-cache'
    $env:npm_config_cache = $env:NPM_CONFIG_CACHE
    $env:NPM_CONFIG_USERCONFIG = Join-Path $Sandbox 'npmrc'
    foreach ($name in @(
            'VIRTUAL_ENV', 'PYTHONHOME', 'HERMES_DESKTOP_HERMES', 'HERMES_TUI_DIR',
            'HERMES_DESKTOP_DEV_SERVER', 'HERMES_DESKTOP_IS_PACKAGED',
            '__HERMES_ACTIVATED', '__HERMES_TEST_PYTHON')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    foreach ($dir in @(
            $env:HERMES_HOME, $env:HERMES_RUNTIME_DIR, $env:HERMES_DESKTOP_USER_DATA_DIR,
            $env:XDG_DATA_HOME, $env:UV_CACHE_DIR, $env:NPM_CONFIG_CACHE)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    if (-not (Test-Path -LiteralPath $env:NPM_CONFIG_USERCONFIG)) {
        New-Item -ItemType File -Path $env:NPM_CONFIG_USERCONFIG | Out-Null
    }
}

function Show-Banner {
    Write-Host "Trial home: $env:HERMES_HOME"
    Write-Host "Desktop data: $env:HERMES_DESKTOP_USER_DATA_DIR"
    Write-Host 'This trial keeps its own profile. Sign in again here.'
    Write-Host "Installed Hermes is left alone. Delete $Sandbox to reset."
    Write-Host 'The first Desktop or TUI launch installs Python and builds the UI.'
}

function Ensure-DesktopPython {
    # `python` on PATH is the tool-store binary. Desktop launches its backend
    # with PYTHONPATH set to the checkout only, which drops the venv. PM's
    # project interpreter is the venv entry; resolving that symlink drops it too.
    if ($env:HERMES_DESKTOP_PYTHON -and (Test-Path -LiteralPath $env:HERMES_DESKTOP_PYTHON)) {
        return
    }
    $py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
    $env:HERMES_DESKTOP_PYTHON = ((& $py -c 'import os; from pathlib import Path; from pm.environments import project_python; print(project_python(Path(os.environ["HERMES_DESKTOP_HERMES_ROOT"])))') | Out-String).Trim()
    if (-not (Test-Path -LiteralPath $env:HERMES_DESKTOP_PYTHON)) {
        throw "no Hermes interpreter at $($env:HERMES_DESKTOP_PYTHON). The trial environment did not finish installing. Re-run this command."
    }
}

function Invoke-Hermes([string[]]$HermesArgs) {
    if ($env:HERMES_TRY_LAUNCHER) {
        & $env:HERMES_TRY_LAUNCHER @HermesArgs
        exit $LASTEXITCODE
    }
    $py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
    & $py -m hermes_cli.main @HermesArgs
    exit $LASTEXITCODE
}

$Surface = $Surface.ToLowerInvariant()
switch ($Surface) {
    { $_ -in @('', '-h', '--help', 'help') } {
        Show-Usage
        if ($Surface -eq '') { exit 1 }
        exit 0
    }
    { $_ -in @('desktop', 'tui', 'cli', 'build', 'exec') } { }
    default {
        Write-Error "unknown command '$Surface'"
        Show-Usage
        exit 1
    }
}

Apply-Isolation

if ($env:TRY_CHECKOUT_SKIP_SETUP -ne '1') {
    Show-Banner
    . (Join-Path $Repo 'activate.ps1')
}

switch ($Surface) {
    'exec' {
        $cmd = @($Rest)
        if ($cmd.Count -gt 0 -and $cmd[0] -eq '--') {
            $cmd = @($cmd | Select-Object -Skip 1)
        }
        if ($cmd.Count -eq 0) { throw 'exec needs a command' }
        & $cmd[0] @($cmd | Select-Object -Skip 1)
        exit $LASTEXITCODE
    }
    { $_ -in @('desktop', 'build') } {
        $rebuild = $false
        foreach ($arg in @($Rest)) {
            if ($arg -eq '--rebuild') { $rebuild = $true }
            elseif ($arg -ne '') { throw "$Surface only accepts --rebuild" }
        }
        Ensure-DesktopPython
        $hermesArgs = @('desktop', '--source', '--ignore-existing', '--hermes-root', $env:HERMES_DESKTOP_HERMES_ROOT)
        if ($Surface -eq 'build') {
            $hermesArgs += '--build-only'
            Write-Host 'Building the trial Desktop.'
        } else {
            Write-Host 'Opening the trial Desktop.'
        }
        if ($rebuild) { $hermesArgs += '--force-build' }
        Invoke-Hermes $hermesArgs
    }
    'tui' {
        Write-Host 'Opening the trial TUI.'
        Invoke-Hermes (@('--tui') + @($Rest))
    }
    'cli' {
        Write-Host 'Opening the trial CLI.'
        Invoke-Hermes @($Rest)
    }
}
