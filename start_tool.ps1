# 一键启动「队伍ID检测工具」
# - 从同目录 .env 读取 STARROCKS_PASSWORD（.env 已被 .gitignore 忽略，不会提交）
# - 本服务器上数据库是 dota2_analysis（不是默认的 dota2_stats），务必带 --database dota2_analysis
# - 监听端口 8050（8000 已被其他服务占用）

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 读取 .env
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)\s*=\s*(.*)\s*$') {
            [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
        }
    }
}

if (-not $env:STARROCKS_PASSWORD) {
    $env:STARROCKS_PASSWORD = Read-Host "请输入 StarRocks 密码"
}

python dota_roster_web.py --listen-port 8050 --database dota2_analysis
