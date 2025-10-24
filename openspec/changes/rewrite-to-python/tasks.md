## 1. 项目结构迁移

- [ ] 1.1 创建 Python 项目结构 (`src/`, `tests/`, `docs/`)
- [ ] 1.2 设置 `pyproject.toml` 和 `requirements.txt`
- [ ] 1.3 配置 Python 代码质量工具 (black, flake8, mypy)
- [ ] 1.4 设置 pytest 测试框架
- [ ] 1.5 更新 `.gitignore` 适配 Python 项目

## 2. 核心 MCP 服务器实现

- [ ] 2.1 实现 Python MCP 服务器基础框架
- [ ] 2.2 实现异步 HTTP 客户端 (使用 aiohttp 或 httpx)
- [ ] 2.3 实现配置管理模块 (环境变量 + 配置文件)
- [ ] 2.4 实现认证和授权机制
- [ ] 2.5 实现统一错误处理和日志记录

## 3. Loki 客户端重写

- [ ] 3.1 实现基于 HTTP API 的 Loki 客户端
- [ ] 3.2 实现 LogQL 查询功能
- [ ] 3.3 实现标签和标签值查询
- [ ] 3.4 实现健康检查功能
- [ ] 3.5 实现租户发现功能
- [ ] 3.6 添加请求重试和超时机制

## 4. MCP 工具实现

- [ ] 4.1 实现 `health_check` 工具 (无参数)
- [ ] 4.2 实现 `get_tenants` 工具 (无参数)
- [ ] 4.3 重写 `query_loki` 工具 (添加 tenant 参数)
- [ ] 4.4 重写 `get_labels` 工具 (添加 tenant 参数)
- [ ] 4.5 重写 `get_label_values` 工具 (添加 tenant 参数)

## 5. 测试和验证

- [ ] 5.1 编写单元测试覆盖所有核心功能
- [ ] 5.2 编写集成测试验证 Loki API 交互
- [ ] 5.3 编写 MCP 工具测试
- [ ] 5.4 性能测试和优化
- [ ] 5.5 错误处理测试

## 6. 文档和部署

- [ ] 6.1 更新 README.md 文档
- [ ] 6.2 更新安装和配置说明
- [ ] 6.3 更新环境变量文档 (移除 LOKI_TENANT_ID)
- [ ] 6.4 创建 Python 包发布配置
- [ ] 6.5 更新 Docker 镜像 (如果存在)

## 7. 清理和迁移

- [ ] 7.1 备份现有 TypeScript 代码
- [ ] 7.2 删除 TypeScript 相关文件
- [ ] 7.3 验证新 Python 实现的功能完整性
- [ ] 7.4 更新 CI/CD 流水线
- [ ] 7.5 更新项目元数据和许可证信息
