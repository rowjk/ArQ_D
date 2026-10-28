# ArQ 開機自動啟動管理
#
# 在「目前使用者」的啟動資料夾放一個捷徑：登入 Windows 後自動執行 run.ps1。
# 同時建立 / 更新桌面捷徑，兩者設定完全相同 (同一個 PowerShell、同樣參數與視窗)，開機與手動啟動的 ArQ 一致。
# 不使用系統服務：截圖與 104 爬蟲需要已登入的桌面環境，且啟動資料夾不需要系統管理員權限、刪掉捷徑即可停用。
#
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 install     # 啟用 (並更新桌面捷徑)
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 uninstall   # 停用 (桌面捷徑保留)
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 status      # 查看狀態

param(
    [ValidateSet('install', 'uninstall', 'status')]
    [string]$Action = 'status'
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root     = Split-Path -Parent $PSScriptRoot
$runPs1   = Join-Path $root 'run.ps1'
$icon     = Join-Path $root 'assets\arq.ico'
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'ArQ.lnk'
$desktop  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ArQ 遠端 AI 中樞.lnk'

# 開機與桌面共用的唯一設定：改這裡兩個捷徑一起變
function Set-ArQShortcut([string]$Path, [string]$Description) {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    $lnk.TargetPath       = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -File `"$runPs1`""
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle      = 7   # 最小化
    $lnk.Description      = $Description
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
}

function Show-ArQShortcut([string]$Label, [string]$Path) {
    if (Test-Path $Path) {
        $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
        Write-Host "🟢 ${Label}：$Path"
        Write-Host "   執行：$($lnk.TargetPath) $($lnk.Arguments)"
    } else {
        Write-Host "⚪ ${Label}：未建立"
    }
}

switch ($Action) {
    'install' {
        Set-ArQShortcut $shortcut 'ArQ Discord AI 助理 (開機自動啟動)'
        Set-ArQShortcut $desktop  'ArQ Discord AI 助理'
        Write-Host "✅ 已啟用開機自動啟動：$shortcut"
        Write-Host "✅ 桌面捷徑已同步為相同設定：$desktop"
    }
    'uninstall' {
        if (Test-Path $shortcut) {
            Remove-Item $shortcut
            Write-Host '✅ 已停用開機自動啟動。'
        } else {
            Write-Host 'ℹ️ 原本就沒有啟用開機自動啟動。'
        }
    }
    'status' {
        Show-ArQShortcut '開機自動啟動' $shortcut
        Show-ArQShortcut '桌面捷徑' $desktop
    }
}
