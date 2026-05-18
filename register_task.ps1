#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Register the Jakevolume daily scheduled task.
    Run once from an elevated PowerShell prompt:
        powershell -ExecutionPolicy Bypass -File register_task.ps1

.NOTES
    - Fires at 07:58 AM local time Mon–Fri.
    - "Run only when user is logged on" keeps it simple (no stored password needed).
    - Change -LogonType to S4U or Password if you need headless/remote execution.
#>

$TaskName   = "Jakevolume Daily"
$ScriptDir  = "C:\Users\malir\Projects\Python\Jakevolume"
$BatFile    = "$ScriptDir\start_jakevolume.bat"

# ── Action: launch the bat file in a visible cmd window ──────────────────────
$Action = New-ScheduledTaskAction `
    -Execute  "cmd.exe" `
    -Argument "/c `"$BatFile`""

# ── Trigger: 07:58 AM every weekday ──────────────────────────────────────────
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "07:58AM"

# ── Settings ──────────────────────────────────────────────────────────────────
# ExecutionTimeLimit: auto-kill at 15:58 CST at the latest (8 h after 7:58 AM)
# MultipleInstances IgnoreNew: don't launch a second copy if one is already running
# StartWhenAvailable: catch up if the PC was asleep or off at 7:58 AM
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -MultipleInstances  IgnoreNew `
    -StartWhenAvailable

# ── Principal: run as current user, highest privilege ────────────────────────
$Principal = New-ScheduledTaskPrincipal `
    -UserId    "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel  Highest

# ── Register (or replace if already exists) ───────────────────────────────────
Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -Principal  $Principal `
    -Description "Start Jakevolume 0DTE alerting system at 7:58 AM on market days" `
    -Force

Write-Host ""
Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "Next run: $(( Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo ).NextRunTime)"
