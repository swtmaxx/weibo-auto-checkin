# 微博超话签到 - 注册"登录时自动启动"的计划任务(当前用户)
# 用法: 右键"使用 PowerShell 运行",或在 PowerShell 中执行:
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1

$ErrorActionPreference = "Stop"
$taskName = "WeiboCheckin"
$startBat = Join-Path $PSScriptRoot "start.bat"

if (-not (Test-Path $startBat)) {
    Write-Error "找不到 $startBat,请确认脚本位于 deploy\windows 目录内。"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$startBat`"" `
    -WorkingDirectory (Split-Path $startBat -Parent)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "微博超话签到 WebUI(登录时自动启动)" -Force | Out-Null

Write-Host "已注册计划任务 '$taskName':下次登录时自动启动签到服务。"
Write-Host "取消自启: schtasks /delete /tn `"$taskName`" /f"
Write-Host "立即启动: schtasks /run /tn `"$taskName`""
