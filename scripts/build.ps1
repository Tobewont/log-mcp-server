# Docker 构建脚本 (PowerShell)

param(
    [string]$Tag = "latest",
    [string]$Name = "loki-mcp-server", 
    [string]$Platform = "linux/amd64",
    [switch]$NoCache,
    [switch]$Push,
    [switch]$Dev,
    [switch]$Help
)

# 帮助信息
function Show-Help {
    Write-Host @"
Docker 构建脚本

用法: .\build.ps1 [选项]

选项:
    -Help               显示帮助信息
    -Tag TAG            设置镜像标签 (默认: latest)
    -Name NAME          设置镜像名称 (默认: loki-mcp-server)
    -Platform PLATFORM  设置目标平台 (默认: linux/amd64)
    -NoCache            不使用缓存构建
    -Push               构建后推送到注册表
    -Dev                开发模式构建（包含开发依赖）

示例:
    .\build.ps1                         # 基本构建
    .\build.ps1 -Tag v1.0.0            # 指定标签
    .\build.ps1 -NoCache -Push          # 无缓存构建并推送
    .\build.ps1 -Dev                    # 开发模式构建
"@
}

if ($Help) {
    Show-Help
    exit 0
}

# 构建参数
$BuildArgs = @()
if ($NoCache) { $BuildArgs += "--no-cache" }
if ($Dev) { $BuildArgs += "--target", "development" }

$FullImageName = "${Name}:${Tag}"

Write-Host "=== Docker 构建开始 ===" -ForegroundColor Green
Write-Host "镜像名称: $FullImageName" -ForegroundColor Yellow
Write-Host "目标平台: $Platform" -ForegroundColor Yellow
Write-Host "构建参数: $($BuildArgs -join ' ')" -ForegroundColor Yellow
Write-Host ""

# 检查 Docker 是否可用
try {
    docker --version | Out-Null
} catch {
    Write-Host "错误: Docker 未安装或不可用" -ForegroundColor Red
    exit 1
}

# 检查 Docker 服务是否运行
try {
    docker info | Out-Null
} catch {
    Write-Host "错误: Docker 服务未运行" -ForegroundColor Red
    exit 1
}

# 切换到项目根目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "切换到项目根目录: $ProjectRoot" -ForegroundColor Yellow

# 构建镜像
Write-Host "开始构建 Docker 镜像..." -ForegroundColor Green

$DockerCmd = @("docker", "build", "--platform", $Platform, "--tag", $FullImageName) + $BuildArgs + @(".")

try {
    & $DockerCmd[0] $DockerCmd[1..($DockerCmd.Length-1)]
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ 镜像构建成功: $FullImageName" -ForegroundColor Green
    } else {
        Write-Host "❌ 镜像构建失败" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "❌ 镜像构建失败: $_" -ForegroundColor Red
    exit 1
}

# 显示镜像信息
Write-Host "=== 镜像信息 ===" -ForegroundColor Green
docker images $Name --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"

# 推送镜像（如果指定）
if ($Push) {
    Write-Host "推送镜像到注册表..." -ForegroundColor Green
    
    try {
        docker push $FullImageName
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ 镜像推送成功" -ForegroundColor Green
        } else {
            Write-Host "❌ 镜像推送失败" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "❌ 镜像推送失败: $_" -ForegroundColor Red
        exit 1
    }
}

Write-Host "=== 构建完成 ===" -ForegroundColor Green
Write-Host "运行镜像: docker run --rm -e LOKI_ADDR=http://loki:3100 $FullImageName" -ForegroundColor Yellow
Write-Host "启动完整环境: docker-compose up -d" -ForegroundColor Yellow
