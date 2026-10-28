# UTF-8 Encoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path $PSScriptRoot

$env:ARQ_SUPERVISED = "1"

Write-Host '==========================================================' -ForegroundColor Cyan
Write-Host 'Starting ArQ Discord Bridge...' -ForegroundColor Green
Write-Host '==========================================================' -ForegroundColor Cyan
Write-Host ''

# 開機自動啟動時網路 (Wi-Fi) 可能還沒連上：最多等 60 秒，能解析 discord.com 再啟動，避免一開機就連線失敗結束
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    try { [System.Net.Dns]::GetHostAddresses('discord.com') | Out-Null; break }
    catch { Write-Host '⏳ 等待網路連線...' -ForegroundColor DarkGray; Start-Sleep -Seconds 3 }
}

do {
    uv run python bot.py
    $code = $LASTEXITCODE
    if ($code -eq 42) {
        Write-Host ''
        Write-Host '🔄 ArQ 請求重新啟動，正在重啟服務...' -ForegroundColor Cyan
        Write-Host ''
        Start-Sleep -Seconds 1
    }
} while ($code -eq 42)

Write-Host ''
Write-Host '==========================================================' -ForegroundColor Yellow
Write-Host 'ArQ Bot stopped.' -ForegroundColor Yellow
Write-Host '==========================================================' -ForegroundColor Yellow
Read-Host 'Press Enter to exit...'
