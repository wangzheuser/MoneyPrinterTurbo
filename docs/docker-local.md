# 本机统一 Docker Compose 部署

项目根目录下的 `docker-compose.override.yml` 自动覆盖默认编排：

- WebUI 与 API 共用本地构建镜像 `moneyprinterturbo-local:latest`。
- Python 3.11 / Debian Bookworm；原 Dockerfile 的默认基础镜像参数保持兼容。
- 代码由 `COPY . .` 打入镜像，不挂载宿主机代码目录。
- 仅挂载 `config.toml` 和 `storage`，重建镜像不覆盖配置、密钥和生成数据。
- 两个服务由 Compose 项目 `moneyprinterturbo-local` 管理。
- WebUI：<http://127.0.0.1:8501>；API 文档：<http://127.0.0.1:18080/docs>。

在项目根目录执行（先构建一次共享镜像）：

```powershell
docker compose build webui
docker compose up -d --no-build
docker compose ps
```

修改 Python 代码后，重新执行上述构建和启动命令。日常启动只需 `docker compose up -d --no-build`。
停止使用 `docker compose stop`，查看日志使用 `docker compose logs --tail 100 webui api`。
本机编排要求支持 `!override` 的 Compose，本次部署使用 Compose v5.3.0。

## 本机网络依赖

构建和运行时均使用 `http://host.docker.internal:7890`，对应本机已验证的代理。
代理必须保持运行。`NO_PROXY` 保留本机与 Compose 服务间直连。
此前容器 DNS 返回 `127.148.*`，直接访问该回环地址失败；代理负责远端域名解析和连接。
此文件为本机专用配置，换机器时按实际出口调整，不复用宿主机的 `127.0.0.1` 作为容器代理。

镜像不包含配置密钥（`.dockerignore` 排除了 `config.toml`）。模型连接和视频生成分别
需要对应的网关配置。HTTP 健康检查只证明服务启动，模型列表检查不等同于实际生成验证。
