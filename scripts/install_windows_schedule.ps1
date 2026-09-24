param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runName = 'PKU Court Booking - Daily Run'
$heartbeatName = 'PKU Court Booking - Heartbeat'
$probeName = 'PKU Court Booking - On-Demand Check-In'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $runName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $heartbeatName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $probeName -Confirm:$false -ErrorAction SilentlyContinue
    return
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python virtual environment was not found at $pythonExe"
}

$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$fireTime = (& $pythonExe -X utf8 -c 'from datetime import datetime; from web.backend.config_loader import load_set; from web.backend.scheduler import compute_next_fire; print(datetime.fromtimestamp(compute_next_fire(load_set("scheduled"))).strftime("%H:%M:%S"))').Trim()
if ($LASTEXITCODE -ne 0 -or $fireTime -notmatch '^\d{2}:\d{2}:\d{2}$') {
    throw 'Could not calculate the scheduled preparation time from config.'
}
$preflightTime = ([datetime]::ParseExact($fireTime, 'HH:mm:ss', $null).AddMinutes(-12)).ToString('HH:mm:ss')

$runAction = New-ScheduledTaskAction -Execute $pythonExe `
    -Argument '-X utf8 -m web.backend.local_schedule' -WorkingDirectory $projectRoot
$runTrigger = New-ScheduledTaskTrigger -Daily -At $fireTime
Register-ScheduledTask -TaskName $runName -Action $runAction -Trigger $runTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null

$heartbeatAction = New-ScheduledTaskAction -Execute $pythonExe `
    -Argument '-X utf8 -m web.backend.schedule_sync heartbeat' -WorkingDirectory $projectRoot
# The preflight check-in syncs config before browser preparation.
$heartbeatTriggers = @('00:00:00', '04:00:00', '08:00:00', $preflightTime, '16:00:00', '20:00:00') |
    ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-ScheduledTask -TaskName $heartbeatName -Action $heartbeatAction `
    -Trigger $heartbeatTriggers -Principal $principal -Settings $settings -Force | Out-Null

# A one-minute SSH poll checks only for dashboard requests. It does not send a
# heartbeat unless requested, so the six routine check-ins remain unchanged.
$probeAction = New-ScheduledTaskAction -Execute $pythonExe `
    -Argument '-X utf8 -m web.backend.schedule_sync probe-checkin' -WorkingDirectory $projectRoot
$probeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$probeSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $probeName -Action $probeAction -Trigger $probeTrigger `
    -Principal $principal -Settings $probeSettings -Force | Out-Null

Get-ScheduledTask -TaskName $runName, $heartbeatName, $probeName |
    Select-Object TaskName, State, @{Name='Triggers';Expression={($_.Triggers.StartBoundary -join ', ')}} |
    Format-Table -AutoSize
