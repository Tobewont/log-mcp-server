# Docker 配置文件

本目录包含 Docker 部署相关的配置文件。

## 文件说明

### `loki-config.yaml`
Loki 服务器的基本配置文件，用于 Docker Compose 环境中的 Loki 实例。

**主要配置项：**
- 监听地址和端口
- 存储配置（本地文件系统）
- 日志级别和格式
- 租户配置

### `grafana-datasources.yaml`
Grafana 数据源配置，自动配置 Loki 作为数据源。

**配置内容：**
- Loki 数据源连接信息
- 访问模式和认证设置
- 默认查询设置

## 使用方法

这些配置文件会在 Docker Compose 启动时自动挂载到相应的容器中：

```bash
# 启动完整的开发环境
docker-compose up -d

# 查看 Loki 日志
docker-compose logs loki

# 查看 Grafana 日志
docker-compose logs grafana
```

## 自定义配置

如需自定义配置，可以：

1. 复制配置文件到项目根目录的 `config/` 目录
2. 修改配置内容
3. 更新 `docker-compose.yml` 中的挂载路径

## 注意事项

- Loki 配置使用本地文件系统存储，适用于开发和测试
- 生产环境建议使用对象存储（如 S3）
- Grafana 默认用户名/密码为 `admin/admin`
- 所有服务都在同一个 Docker 网络中，可以通过服务名互相访问
