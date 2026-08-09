# Start the orchestrator in the foreground. Ctrl+C to stop.
# Task Scheduler uses run-hidden.ps1 instead; this one is for debugging, because
# a hidden orchestrator that fails to start looks identical to a missing tool.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m orchestrator.server
