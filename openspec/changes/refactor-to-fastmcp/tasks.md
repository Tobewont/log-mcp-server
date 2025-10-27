# 实施任务清单

## 1. 依赖和配置更新

- [x] 1.1 更新 requirements.txt 添加 FastMCP 依赖
- [x] 1.2 更新 pyproject.toml 配置
- [x] 1.3 简化配置管理，移除服务器模式配置
- [x] 1.4 添加 FastMCP 特定配置选项
- [x] 1.5 更新环境变量示例文件

## 2. 工具重构

- [x] 2.1 重构 health_check 工具使用 FastMCP 装饰器
- [x] 2.2 重构 get_tenants 工具使用 FastMCP 装饰器
- [x] 2.3 重构 query_loki 工具使用 FastMCP 装饰器
- [x] 2.4 重构 get_labels 工具使用 FastMCP 装饰器
- [x] 2.5 重构 get_label_values 工具使用 FastMCP 装饰器

## 3. 主程序重写

- [x] 3.1 创建新的 FastMCP 主程序
- [x] 3.2 集成 Loki 客户端和配置
- [x] 3.3 注册所有工具到 FastMCP 实例
- [x] 3.4 配置 FastMCP 服务器选项
- [x] 3.5 添加启动和关闭处理

## 4. 清理旧代码

- [x] 4.1 移除 server/ 目录及其所有文件
- [x] 4.2 清理 main.py 中的旧实现
- [x] 4.3 移除不再需要的工具基类
- [x] 4.4 更新 __init__.py 文件
- [x] 4.5 清理导入语句

## 5. Docker 和部署更新

- [x] 5.1 更新 Dockerfile 移除复杂的服务器配置
- [x] 5.2 更新 docker-compose.yml 使用 FastMCP 默认端口
- [x] 5.3 简化环境变量配置
- [x] 5.4 更新健康检查配置
- [x] 5.5 测试容器化部署

## 6. 测试和验证

- [x] 6.1 测试 stdio 模式功能
- [x] 6.2 测试 HTTP/SSE 模式功能
- [x] 6.3 验证所有工具正常工作
- [x] 6.4 测试 Loki 连接和认证
- [x] 6.5 验证 Docker 部署

## 7. 文档更新

- [x] 7.1 更新 README.md 说明 FastMCP 架构
- [x] 7.2 更新配置文档
- [x] 7.3 添加 FastMCP Web UI 使用说明
- [x] 7.4 更新故障排除指南
- [x] 7.5 更新 API 文档

## 8. 性能优化和测试

- [x] 8.1 性能基准测试
- [x] 8.2 内存使用优化
- [x] 8.3 并发处理测试
- [x] 8.4 错误处理验证
- [x] 8.5 日志记录优化
