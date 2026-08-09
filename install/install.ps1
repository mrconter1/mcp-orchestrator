# Install the orchestrator: a Scheduled Task at logon, plus the Claude Code hooks.
#
# No admin rights needed - the task runs as the logged-on user. That is also why
# this is a Scheduled Task and not a Windows Service: services run in session 0,
# isolated from the desktop, so a service-hosted orchestrator could never show a
# tray icon or open a terminal window.
#
#   .\install\install.ps1            # register and start
#   .\install\install.ps1 -NoStart   # register only
#   .\install\install.ps1 -TaskOnly  # skip the settings.json hook wiring

[CmdletBinding()]
param(
    [switch]$NoStart,
    [switch]$TaskOnly,
    [string]$TaskName = "MCP Orchestrator"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonw)) {
    throw "No venv found at $pythonw. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$who = "$env:USERDOMAIN\$env:USERNAME"

# pythonw.exe, not python.exe: it is the windowed interpreter, so the
# orchestrator runs with no console window of its own. The tray icon is what
# makes it visible instead.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m orchestrator.server" -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $who
# Let the desktop settle before claiming a tray slot.
$trigger.Delay = "PT10S"

# Interactive, not ServiceAccount: the process has to live in the user's desktop
# session for the tray icon to exist at all.
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Runs the local MCP servers and keeps them alive. Repo: $repo" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (at logon, as $who)" -ForegroundColor Green

if (-not $TaskOnly) {
    Write-Host "`nWiring Claude Code hooks..." -ForegroundColor Cyan
    & $python -m orchestrator.install install
}

if (-not $NoStart) {
    Write-Host "`nStarting..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Task state: $state"
    & $python -c "from orchestrator import supervise; print('orchestrator port 8768:', 'listening' if supervise.port_open(8768) else 'NOT listening - check the tray icon and the logs')"
}

Write-Host "`nDone. New sessions get the hooks; sessions already running do not." -ForegroundColor Green
Write-Host "Uninstall with: .\install\uninstall.ps1"
