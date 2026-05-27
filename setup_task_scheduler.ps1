# Registers a Windows Task Scheduler job to run Jakevolume at 8:10 AM daily.
# run_scheduled.bat is a watchdog: it auto-restarts main.py on crash every 5
# minutes until 15:15 local time. RestartCount=1 is a safety net if cmd.exe itself fails.
#
# Run this script once as Administrator:
#   Right-click PowerShell > "Run as administrator"
#   Then: .\setup_task_scheduler.ps1

$TaskName    = "Jakevolume_DailyRun"
$BatchFile   = "C:\Users\malir\Projects\Python\Jakevolume\run_scheduled.bat"
$TriggerTime = "08:10"

# Remove existing task with the same name (clean re-registration)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchFile`""
$trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 10) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# Run as the current logged-in user so env vars / credentials are available
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Description "Runs Jakevolume 0DTE alerting at 8:10 AM; watchdog in bat restarts on crash every 5 min until 15:15"

Write-Host ""
Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "Runs: daily at $TriggerTime"
Write-Host "Batch: $BatchFile"
Write-Host "Log:   C:\Users\malir\Projects\Python\Jakevolume\jakevolume_scheduled.log"
Write-Host ""
Write-Host "To verify: Get-ScheduledTask -TaskName '$TaskName' | fl"
Write-Host "To run now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
