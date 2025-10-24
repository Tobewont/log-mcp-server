## 1. 项目结构迁移

- [x] 1.1 创建 Python 项目结构 (`src/`, `tests/`, `docs/`)
- [x] 1.2 设置 `pyproject.toml` 和 `requirements.txt`
- [x] 1.3 配置 Python 代码质量工具 (black, flake8, mypy)
- [x] 1.4 设置 pytest 测试框架
- [x] 1.5 更新 `.gitignore` 适配 Python 项目

## 2. 核心 MCP 服务器实现

- [x] 2.1 实现 Python MCP 服务器基础框架
- [x] 2.2 实现异步 HTTP 客户端 (使用 aiohttp 或 httpx)
- [x] 2.3 实现配置管理模块 (环境变量 + 配置文件)
- [x] 2.4 实现认证和授权机制
- [x] 2.5 实现统一错误处理和日志记录

## 3. Loki 客户端重写

- [x] 3.1 实现基于 HTTP API 的 Loki 客户端
- [x] 3.2 实现 LogQL 查询功能
- [x] 3.3 实现标签和标签值查询
- [x] 3.4 实现健康检查功能
- [x] 3.5 实现租户发现功能
- [x] 3.6 添加请求重试和超时机制

## 4. MCP 工具实现

- [x] 4.1 实现 `health_check` 工具 (无参数)
- [x] 4.2 实现 `get_tenants` 工具 (无参数)
- [x] 4.3 重写 `query_loki` 工具 (添加 tenant 参数)
- [x] 4.4 重写 `get_labels` 工具 (添加 tenant 参数)
- [x] 4.5 重写 `get_label_values` 工具 (添加 tenant 参数)

## 5. 测试和验证

- [x] 5.1 编写单元测试覆盖所有核心功能
- [ ] 5.2 编写集成测试验证 Loki API 交互
- [ ] 5.3 编写 MCP 工具测试
- [ ] 5.4 性能测试和优化
- [ ] 5.5 错误处理测试

## 6. 文档和部署

- [x] 6.1 更新 README.md 文档
- [x] 6.2 更新安装和配置说明
- [x] 6.3 更新环境变量文档 (移除 LOKI_TENANT_ID)
- [x] 6.4 创建 Python 包发布配置
- [ ] 6.5 更新 Docker 镜像 (如果存在)

## 7. 清理和迁移

- [x] 7.1 备份现有 TypeScript 代码
- [x] 7.2 删除 TypeScript 相关文件
- [x] 7.3 验证新 Python 实现的功能完整性
- [ ] 7.4 更新 CI/CD 流水线
- [ ] 7.5 更新项目元数据和许可证信息
