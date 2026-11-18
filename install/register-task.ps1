# Registers the Windows Task Scheduler entry "ShortPost Run" (CLAUDE.md 12).
# Runs `shortpost run` at logon and every 15 minutes, only while you are logged on (YouTube browser mode needs Chrome).
# Usage: powershell -ExecutionPolicy Bypass -File install\register-task.ps1 [-Remove]
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$name = "ShortPost Run"
$repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$name' (if it existed)."
    return
}

$pyw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "Missing $pyw. Run install\install.ps1 first." }

$a  = New-ScheduledTaskAction -Execute $pyw -Argument "-m shortpost run" -WorkingDirectory $repo
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$s  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$p  = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $name -Action $a -Trigger @($t1, $t2) -Settings $s -Principal $p `
    -Description "Posts scheduled Shorts (shortpost run)" -Force | Out-Null
Write-Host "Registered '$name': at logon and every 15 minutes. Check with: Get-ScheduledTask '$name'"
