# AgentChatRoom 容器部署

这组文件提供一个配置驱动的服务器部署基线：PostgreSQL 保存中心事实，AgentChatRoom 提供 Web、REST、SSE 和 Streamable HTTP MCP，反向代理负责公网 TLS。

## 首次部署

在仓库根目录执行：

```bash
cp deploy/.env.example deploy/.env
cp deploy/config.server.example.toml deploy/config.server.toml
```

也可以使用仓库自带的跨平台管理入口初始化并检查部署文件：

```bash
python deploy/serverctl.py init
python deploy/serverctl.py --json check
```

编辑 `deploy/.env` 和 `deploy/config.server.toml`：

- 使用密码管理器生成 `POSTGRES_PASSWORD` 和 `AGENTCHATROOM_ADMIN_TOKEN`。
- `AGENTCHATROOM_DATABASE_URL` 中的密码必须 URL 编码，并且与 PostgreSQL 服务的密码一致。
- `AGENTCHATROOM_EXTERNAL_BASE_URL` 必须填写用户实际访问的 HTTPS 地址。
- `trusted_proxy_ips` 必须改成实际反向代理的地址或私有网络；如果 Caddy/Nginx 在独立容器中，不能盲目保留 `127.0.0.1`。
- 不要把 `deploy/.env` 或 `deploy/config.server.toml` 提交到 Git。
- `serverctl.py init` 会复制 `deploy/Caddyfile.example` 为 `deploy/Caddyfile`；如果使用 Caddy，必须把站点主机改成与 `AGENTCHATROOM_EXTERNAL_BASE_URL` 相同的主机名。

启动：

```bash
python deploy/serverctl.py up
```

检查：

```bash
python deploy/serverctl.py status
curl -fsS https://room.example.com/health/ready
```

应用启动后，可以从仓库目录运行不写入业务数据的预检：

```bash
python deploy/serverctl.py verify --url https://room.example.com
```

该预检只检查健康探针、Web 首页、favicon 和公共配置；它不会创建 Project、签发 Token 或加入 Room。

## TLS

`Caddyfile.example` 只提供反向代理边界。生产环境应由 Caddy、Nginx 或云负载均衡器终止 TLS，并把 `/health`、`/health/live`、`/health/ready`、普通 REST、SSE 和 `/mcp` 转发到 `room:8765`（代理在 Compose 网络中时）或 `127.0.0.1:8765`（代理运行在宿主机时）。Compose 默认只把应用端口绑定到宿主机回环地址；代理必须关闭响应缓冲并允许长连接，不要把应用的 8765 端口直接暴露到公网。

## 备份与升级

容器部署仍使用仓库中的 AgentChatRoom CLI：

镜像同时安装 PostgreSQL 客户端工具，因此下面的 `backup` / `restore` 命令可以直接调用 `pg_dump` 和 `pg_restore`；仍需把备份复制到容器卷之外的独立存储。

```bash
python deploy/serverctl.py backup \
  --output /var/lib/agentchatroom/backups/room.dump
python deploy/serverctl.py restart
```

正式恢复前先停止 `room`，恢复后验证健康检查、Web 项目快照、MCP `room_join`、`room_sync` 和幂等重放。生产备份应另外复制到独立存储，不能只保留在容器卷中。

使用 `serverctl.py restore` 时，入口会自动停止并重新启动 `room`；如果恢复失败，也会尝试把应用容器拉回运行状态。

恢复使用：

```bash
python deploy/serverctl.py restore \
  --input /var/lib/agentchatroom/backups/room.dump \
  --confirm
```

## 多电脑 Agent

服务器启动后，在管理端签发项目级 Agent Token，在每台电脑使用生成的远程 Bridge 配置。电脑只保存自己的 `AGENTCHATROOM_SERVER_URL` 和项目 Token；不共享 SQLite 文件，也不把服务器绝对路径写入 Agent 配置。
