#!/bin/bash
# Docker 构建脚本

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 默认配置
IMAGE_NAME="loki-mcp-server"
TAG="latest"
PLATFORM="linux/amd64"
BUILD_ARGS=""

# 帮助信息
show_help() {
    cat << EOF
Docker 构建脚本

用法: $0 [选项]

选项:
    -h, --help          显示帮助信息
    -t, --tag TAG       设置镜像标签 (默认: latest)
    -n, --name NAME     设置镜像名称 (默认: loki-mcp-server)
    -p, --platform PLATFORM  设置目标平台 (默认: linux/amd64)
    --no-cache          不使用缓存构建
    --push              构建后推送到注册表
    --dev               开发模式构建（包含开发依赖）

示例:
    $0                          # 基本构建
    $0 -t v1.0.0               # 指定标签
    $0 --no-cache --push       # 无缓存构建并推送
    $0 --dev                   # 开发模式构建
EOF
}

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -t|--tag)
            TAG="$2"
            shift 2
            ;;
        -n|--name)
            IMAGE_NAME="$2"
            shift 2
            ;;
        -p|--platform)
            PLATFORM="$2"
            shift 2
            ;;
        --no-cache)
            BUILD_ARGS="$BUILD_ARGS --no-cache"
            shift
            ;;
        --push)
            PUSH=true
            shift
            ;;
        --dev)
            BUILD_ARGS="$BUILD_ARGS --target development"
            shift
            ;;
        *)
            echo -e "${RED}错误: 未知参数 $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# 构建信息
FULL_IMAGE_NAME="${IMAGE_NAME}:${TAG}"

echo -e "${GREEN}=== Docker 构建开始 ===${NC}"
echo -e "镜像名称: ${YELLOW}${FULL_IMAGE_NAME}${NC}"
echo -e "目标平台: ${YELLOW}${PLATFORM}${NC}"
echo -e "构建参数: ${YELLOW}${BUILD_ARGS}${NC}"
echo

# 检查 Docker 是否可用
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: Docker 未安装或不可用${NC}"
    exit 1
fi

# 检查 Docker 服务是否运行
if ! docker info &> /dev/null; then
    echo -e "${RED}错误: Docker 服务未运行${NC}"
    exit 1
fi

# 切换到项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo -e "${YELLOW}切换到项目根目录: ${PROJECT_ROOT}${NC}"

# 构建镜像
echo -e "${GREEN}开始构建 Docker 镜像...${NC}"
docker build \
    --platform "$PLATFORM" \
    --tag "$FULL_IMAGE_NAME" \
    $BUILD_ARGS \
    .

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ 镜像构建成功: ${FULL_IMAGE_NAME}${NC}"
else
    echo -e "${RED}❌ 镜像构建失败${NC}"
    exit 1
fi

# 显示镜像信息
echo -e "${GREEN}=== 镜像信息 ===${NC}"
docker images "$IMAGE_NAME" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"

# 推送镜像（如果指定）
if [ "$PUSH" = true ]; then
    echo -e "${GREEN}推送镜像到注册表...${NC}"
    docker push "$FULL_IMAGE_NAME"
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ 镜像推送成功${NC}"
    else
        echo -e "${RED}❌ 镜像推送失败${NC}"
        exit 1
    fi
fi

echo -e "${GREEN}=== 构建完成 ===${NC}"
echo -e "运行镜像: ${YELLOW}docker run --rm -e LOKI_ADDR=http://loki:3100 ${FULL_IMAGE_NAME}${NC}"
echo -e "启动完整环境: ${YELLOW}docker-compose up -d${NC}"
