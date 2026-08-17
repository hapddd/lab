# Windows: 备份最近的 ChatGPT 对话（含图片）为 Markdown。
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -Limit 50 -InstallTask
#
# -InstallTask 会注册一个每天 21:00 运行的计划任务。

param(
    [int]$Limit = 20,
    [string]$OutDir = "",
    [switch]$InstallTask,
    [switch]$UninstallTask,
    [string]$TaskTime = "21:00"
)

$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $PSScriptRoot
$TaskName = "ChatGPT 聊天记录备份"

function Get-PythonCommand {
    foreach ($candidate in @("python3", "python", "py")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            $version = & $found.Source -c "import sys; print(1 if sys.version_info >= (3, 8) else 0)" 2>$null
            if ($version -eq "1") { return $found.Source }
        }
    }
    throw "找不到 Python 3.8+，请先从 https://www.python.org/downloads/ 安装。"
}

if ($UninstallTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除计划任务。"
    exit 0
}

if ($InstallTask) {
    $arguments = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Limit $Limit"
    if ($OutDir) { $arguments += " -OutDir `"$OutDir`"" }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Daily -At $TaskTime
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "每天备份最近的 ChatGPT 对话为 Markdown" -Force | Out-Null
    Write-Host "已注册计划任务「$TaskName」，每天 $TaskTime 运行。"
    Write-Host "立刻跑一次: Start-ScheduledTask -TaskName '$TaskName'"
    exit 0
}

if (-not $OutDir) {
    $documents = [Environment]::GetFolderPath("MyDocuments")
    if (-not $documents) { $documents = Join-Path $env:USERPROFILE "Documents" }
    $OutDir = Join-Path $documents "chat_bak"
}

$logDir = Join-Path $OutDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("backup-" + (Get-Date -Format "yyyyMMdd") + ".log")

$python = Get-PythonCommand
$env:PYTHONPATH = $PackageRoot + [IO.Path]::PathSeparator + $env:PYTHONPATH
$env:PYTHONUTF8 = "1"

Write-Host "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 开始备份 ===="
& $python -m chatgpt_backup backup --out $OutDir --limit $Limit --log-file $logFile
$status = $LASTEXITCODE
Write-Host "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 结束，退出码 $status ===="

if ($status -eq 2) {
    Write-Warning @"
登录状态不可用。请任选一种方式处理:
  1) 重新读取登录状态: $python -m chatgpt_backup login
  2) 手动粘贴凭证:     $python -m chatgpt_backup login --paste
  3) 离线方案: 在 ChatGPT 里「设置 → 数据管理 → 导出数据」，下载后执行
     $python -m chatgpt_backup import <导出包.zip>
"@
}

exit $status
