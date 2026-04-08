# Register Windows Scheduled Task for ashare-system-v2 auto-start
# Uses VBScript wrapper to hide all windows completely
# No admin required

$TaskName = "ashare-system-v2"
$VbsPath = "D:\Coding\lhjy\ashare-system-v2\scripts\start_silent.vbs"

# Remove old task if exists
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Old task removed."

# Action: run VBScript (wscript.exe runs silently with no window)
$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$VbsPath`"" `
    -WorkingDirectory "D:\Coding\lhjy\ashare-system-v2"

# Trigger: run at user logon
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 3) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# Run as current user
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Register
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "ashare-system-v2 auto-start (silent)"

Write-Host ""
Write-Host "Registered: $TaskName"
Write-Host "Launcher: wscript.exe (no window)"
Write-Host ""
Write-Host "Test now (no window will appear):"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "After 30s check health:"
Write-Host "  Invoke-RestMethod http://127.0.0.1:8100/health   # Windows 本机检查"
Write-Host "  WSL 侧请使用 scripts/ashare_api.sh GET /health   # 动态探测 manifest 地址"
Write-Host ""
Write-Host "View log:"
Write-Host "  Get-Content D:\Coding\lhjy\ashare-system-v2\logs\startup.log -Tail 30 -Wait"
