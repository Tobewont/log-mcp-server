# Docker Compose 开发环境管理脚本

param(
    [Parameter(Position=0)]
    [ValidateSet("up", "down", "restart", "logs", "status", "clean", "help")]
    [string]$Action = "help",
    
    [switch]$Build,
    [switch]$Detach,
    [string]$Service = "",
    [switch]$Help
)

function Show-Help {
    Write-Host @"
Docker Compose 开发环境管理脚本

用法: .\dev-env.ps1 <action> [选项]

Actions:
    up          启动开发环境
    down        停止开发环境
    restart     重启开发环境
    logs        查看日志
    status      查看服务状态
    clean       清理环境（删除容器、网络、卷）
    help        显示帮助信息

选项:
    -Build      启动时重新构建镜像
    -Detach     后台运行（仅用于 up）
    -Service    指定服务名称（用于 logs、restart）
    -Help       显示帮助信息

示例:
    .\dev-env.ps1 up                    # 启动开发环境
    .\dev-env.ps1 up -Build -Detach     # 重新构建并后台启动
    .\dev-env.ps1 logs loki-mcp-server  # 查看 MCP 服务器日志
    .\dev-env.ps1 restart loki          # 重启 Loki 服务
    .\dev-env.ps1 down                  # 停止环境
    .\dev-env.ps1 clean                 # 完全清理环境

服务列表:
    - loki-mcp-server   MCP 服务器
    - loki              Loki 日志系统
    - grafana           Grafana 可视化
"@
}

if ($Help -or $Action -eq "help") {
    Show-Help
    exit 0
}

# 检查 Docker Compose 是否可用
try {
    docker-compose --version | Out-Null
} catch {
    try {
        docker compose version | Out-Null
        $ComposeCmd = "docker compose"
    } catch {
        Write-Host "错误: Docker Compose 未安装或不可用" -ForegroundColor Red
        exit 1
    }
}

if (-not $ComposeCmd) {
    $ComposeCmd = "docker-compose"
}

# 切换到项目根目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Yellow

# 检查必要文件
$RequiredFiles = @("docker-compose.yml", "env.example")
foreach ($file in $RequiredFiles) {
    if (-not (Test-Path $file)) {
        Write-Host "错误: 缺少必要文件 $file" -ForegroundColor Red
        exit 1
    }
}

# 检查 .env 文件
if (-not (Test-Path ".env")) {
    Write-Host "警告: .env 文件不存在，将使用 env.example" -ForegroundColor Yellow
    Copy-Item "env.example" ".env"
    Write-Host "已创建 .env 文件，请根据需要修改配置" -ForegroundColor Green
}

# 执行操作
switch ($Action) {
    "up" {
        Write-Host "启动开发环境..." -ForegroundColor Green
        
        $Args = @()
        if ($Build) { $Args += "--build" }
        if ($Detach) { $Args += "-d" }
        
        $Cmd = "$ComposeCmd up $($Args -join ' ')"
        Write-Host "执行: $Cmd" -ForegroundColor Cyan
        
        Invoke-Expression $Cmd
        
        if ($LASTEXITCODE -eq 0 -and $Detach) {
            Write-Host ""
            Write-Host "=== 开发环境已启动 ===" -ForegroundColor Green
            Write-Host "Loki MCP Server: 容器内运行" -ForegroundColor Yellow
            Write-Host "Loki API: http://localhost:3100" -ForegroundColor Yellow
            Write-Host "Grafana: http://localhost:3000 (admin/admin)" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "查看日志: .\dev-env.ps1 logs" -ForegroundColor Cyan
            Write-Host "停止环境: .\dev-env.ps1 down" -ForegroundColor Cyan
        }
    }
    
    "down" {
        Write-Host "停止开发环境..." -ForegroundColor Yellow
        Invoke-Expression "$ComposeCmd down"
    }
    
    "restart" {
        if ($Service) {
            Write-Host "重启服务: $Service" -ForegroundColor Yellow
            Invoke-Expression "$ComposeCmd restart $Service"
        } else {
            Write-Host "重启所有服务..." -ForegroundColor Yellow
            Invoke-Expression "$ComposeCmd restart"
        }
    }
    
    "logs" {
        if ($Service) {
            Write-Host "查看服务日志: $Service" -ForegroundColor Cyan
            Invoke-Expression "$ComposeCmd logs -f $Service"
        } else {
            Write-Host "查看所有服务日志..." -ForegroundColor Cyan
            Invoke-Expression "$ComposeCmd logs -f"
        }
    }
    
    "status" {
        Write-Host "服务状态:" -ForegroundColor Green
        Invoke-Expression "$ComposeCmd ps"
        
        Write-Host ""
        Write-Host "网络信息:" -ForegroundColor Green
        docker network ls | Select-String "loki-mcp"
        
        Write-Host ""
        Write-Host "卷信息:" -ForegroundColor Green
        docker volume ls | Select-String "loki-mcp"
    }
    
    "clean" {
        Write-Host "清理开发环境..." -ForegroundColor Red
        Write-Host "这将删除所有容器、网络和卷！" -ForegroundColor Red
        
        $Confirm = Read-Host "确认清理？(y/N)"
        if ($Confirm -eq "y" -or $Confirm -eq "Y") {
            Invoke-Expression "$ComposeCmd down -v --remove-orphans"
            
            # 清理相关镜像（可选）
            $CleanImages = Read-Host "是否清理相关镜像？(y/N)"
            if ($CleanImages -eq "y" -or $CleanImages -eq "Y") {
                docker images | Select-String "loki-mcp" | ForEach-Object {
                    $ImageId = ($_ -split '\s+')[2]
                    docker rmi $ImageId -f
                }
            }
            
            Write-Host "环境清理完成" -ForegroundColor Green
        } else {
            Write-Host "取消清理" -ForegroundColor Yellow
        }
    }
    
    default {
        Write-Host "错误: 未知操作 '$Action'" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
