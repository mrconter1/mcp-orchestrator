# Remove the scheduled task and the Claude Code hooks.
#
# Leaves the managed servers' own repos, servers.json and the logs alone; this
# undoes the installation, not the work.
#
#   .\install\uninstall.ps1
#   .\install\uninstall.ps1 -KeepHooks

[CmdletBinding()]
param(
    [switch]$KeepHooks,
    [string]$TaskName = "MCP Orchestrator"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $TaskName }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'" -ForegroundColor Green
} else {
    Write-Host "No scheduled task named '$TaskName'"
}

# The task only starts the orchestrator; it does not own the process that is
# already running, so unregistering the task leaves it up.
Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($repo) } |
    ForEach-Object {
        Write-Host "Stopping orchestrator process $($_.Id)"
        Stop-Process -Id $_.Id -Force
    }

if (-not $KeepHooks) {
    if (Test-Path $python) { & $python -m orchestrator.install uninstall }
}

Write-Host "`nDone. Managed servers keep running; stop them with mcp_stop or by hand." -ForegroundColor Green
