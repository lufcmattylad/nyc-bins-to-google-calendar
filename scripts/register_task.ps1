# Registers a Windows scheduled task that runs the bin calendar sync daily.
# Run from the project root:  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
param(
    [string]$TaskName = "BinCalendarSync",
    [string]$At = "07:00"
)

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"   # pythonw = no console window
$Script = Join-Path $ProjectDir "bin_calendar_sync.py"

if (-not (Test-Path $Python)) { throw "Virtualenv not found at $Python - create it first (see README.md)." }

$Action   = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectDir
$Trigger  = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: if the PC was off/asleep at the scheduled time, run as soon as possible after.
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Sync North Yorkshire bin collection dates to Google Calendar" -Force | Out-Null

Write-Host "Registered '$TaskName' to run daily at $At. Logs: $(Join-Path $ProjectDir 'bin_sync.log')"
