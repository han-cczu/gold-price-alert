# 部署指南

本文按当前 [docker-compose.yml](../docker-compose.yml)、[Dockerfile](../Dockerfile) 和 [CLI](../src/gold_monitor/cli.py) 编写。应用是一个内建采集、告警和定时分析的 Web 服务。

本轮尚未完成 Docker 构建与运行验收：本机 Docker Desktop 引擎启动失败，客户端找不到 `dockerDesktopLinuxEngine` 管道。下列命令是当前配置对应的操作方式，不代表容器已经通过运行验证。已执行的检查和剩余事项见 [重构验证记录](refactor-validation.md)。

## 1. 运行方式

### 本地 Python

在项目目录安装并启动：

```bash
pip install -e .
gold-monitor --host 127.0.0.1 --port 8000
```

`gold-monitor` 支持 `--host`、`--port`、`--reload`、`--version`。默认监听 `0.0.0.0:8000`；`--reload` 用于开发。兼容 ASGI 入口为 `gold_monitor.web:app`。

使用**单进程、单 worker、一个实例写入数据库和模型配置文件**。多个应用实例会各自启动采集、告警和定时分析；当前缓存、限流、WebSocket 连接及锁仅在进程内生效，不提供多实例协调。无需另启采集进程。

### Docker Compose

先复制生产模板为 `.env`：

```bash
# Linux/macOS shell
cp .env.production.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.production.example .env
```

编辑 `.env`，替换模板中的凭证占位值。没有使用的通知渠道应清空其示例地址和凭证，避免被识别为已配置渠道。然后执行：

```bash
docker compose up -d --build gold-monitor
docker compose logs -f gold-monitor
```

访问 [本地页面](http://localhost:8000)。`gold-monitor` 容器本身已经运行采集、告警及定时任务。

当前 Compose 服务只有：

| 服务 | 作用 | 启动条件与端口 |
|------|------|----------------|
| `gold-monitor` | Web、采集、告警、分析 | 默认服务；容器 8000 映射到宿主机 `${PORT:-8000}` |
| `nginx` | HTTP/HTTPS 反向代理 | `production` profile；宿主机 80/443 |

启用反向代理：

```bash
docker compose --profile production up -d --build
```

`production` profile 仅启用 Nginx；HTTPS 还需按后文配置证书和 `nginx.conf`。当前没有独立采集服务或采集 profile。

## 2. 配置与持久化

### 管理员凭证和稳定主密钥

当前 Compose 默认 `GOLD_ENABLE_AUTH=true`、`GOLD_ENCRYPT_API_KEYS=true`。部署前配置以下值：

| 变量 | 作用 |
|------|------|
| `GOLD_ADMIN_API_KEY` | 管理接口请求凭证，对应 `X-Admin-Key` |
| `GOLD_SECRET_KEY` | 读取和保存加密配置使用的主密钥，须跨重启保持相同 |
| `GOLD_LLM_CONFIG_PATH` | 模型配置文件路径，Compose 默认 `/app/data/llm_config.json` |
| `GOLD_CORS_ALLOW_ORIGINS` | 允许跨域的来源；空值按当前同源使用 |
| `GOLD_RATE_LIMIT_PER_MINUTE` | 每分钟请求限制，Compose 默认 60 |

可在本机分别生成两个随机值填入管理员凭证和主密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

每次运行生成一个新值。主密钥设置完成后应妥善保管；已有密文配置依赖原值，重启或换机器时不能重新生成后直接替换。更改管理员 API Key 后，在页面设置中重新填写新凭证；页面不会把该值写入浏览器持久存储。

### 当前 Compose 实际使用的路径

| 内容 | 容器内位置 | 持久化情况 |
|------|------------|------------|
| SQLite 数据库 | `/app/data/gold_prices.db` | `gold_data` named volume 挂载 `/app/data` |
| 模型配置 | 默认 `/app/data/llm_config.json` | 使用默认路径时包含在 `gold_data` 内 |
| API 创建的备份 | 默认 `/app/backups/` | 当前 Compose 没有挂载该目录，需要导出备份或自行配置持久目录 |
| Nginx 配置 | `/etc/nginx/nginx.conf` | 从项目 `nginx.conf` 只读挂载 |
| TLS 证书 | `/etc/nginx/ssl/` | 从项目 `ssl/` 只读挂载 |

Compose 明确设置 `GOLD_DATABASE_URL=sqlite:///data/gold_prices.db`。仅修改宿主机 `.env` 中同名值不会替换这一固定项；本地 Python 运行才会直接使用本地 Settings 读取的数据库 URL。生产模板中的其他数据库示例不表示当前 Compose 已配置或验证该数据库。

Compose 的 `.env` 用于替换文件中明确引用的 `${...}`。它不会把所有 `GOLD_*` 自动注入容器；例如当前文件没有透传 `GOLD_BACKUP_PATH` 或数据保留参数。需要调整这类配置时，应在自己的 Compose 覆盖文件中显式添加环境项和必要的目录挂载。

### 行情、分析和通知

- `GOLD_DATA_SOURCE` 支持 `mock`、`sina`、`goldapi`、`fallback`；GoldAPI 使用 `GOLD_GOLDAPI_KEY`，采集间隔使用 `GOLD_FETCH_INTERVAL`。Mock 为模拟数据，不是实时行情。
- 首次没有模型配置文件时，Settings 根据 `GOLD_LLM_PROVIDER`、`GOLD_ANTHROPIC_API_KEY`、`GOLD_OPENAI_API_KEY` 等初始化默认配置；无有效密钥时使用 Mock。
- 已保存模型文件的优先级高于 Settings 默认值。修改环境变量不会自动改写已保存的平台选择；可在页面切换/重置模型配置。代码中显式传入 provider 时，显式选择优先。
- `GOLD_OPENAI_BASE_URL` 配置兼容端点；`GOLD_TAVILY_API_KEY` 用于已实现的搜索路径。模型是否真正搜索以响应中的标记和来源为准。
- 邮件使用 `GOLD_SMTP_*` 与 `GOLD_ALERT_EMAIL_TO`；Webhook 使用 `GOLD_WEBHOOK_URL/GOLD_WEBHOOK_TYPE`；Telegram 使用 `GOLD_TELEGRAM_BOT_TOKEN/GOLD_TELEGRAM_CHAT_ID`。上述项已在当前 Compose 中透传。
- 通知以 Settings 为默认值，再应用数据库保存的启用状态和字段覆盖。页面保存、测试发送和实际告警使用同一解析路径；启用某渠道后再用测试功能检查该部署环境下的真实连通性。

详细优先级、币种和时间约定见 [README](../README.md) 与 [验证记录](refactor-validation.md)。

## 3. HTTPS 与 Nginx

当前仓库的 `nginx.conf` 默认在 80 端口代理应用，443 的 HTTPS server 段被注释；启用 `production` profile 本身不会自动获得证书或打开 HTTPS。

准备与域名匹配的有效证书，将文件放在项目 `ssl/` 下：

- `ssl/fullchain.pem`
- `ssl/privkey.pem`

编辑 [nginx.conf](../nginx.conf)：启用已有 HTTPS server 段，填写实际域名，保持证书路径 `/etc/nginx/ssl/fullchain.pem` 和 `/etc/nginx/ssl/privkey.pem`。需要 HTTP 跳转 HTTPS 时，用已有重定向配置替换 HTTP server 中的直接代理 `location /`，避免保留两个同名 location。

Nginx 已配置向 `gold-monitor:8000` 代理并转发 WebSocket Upgrade。修改配置后检查并重载：

```bash
docker compose --profile production exec nginx nginx -t
docker compose --profile production exec nginx nginx -s reload
```

证书签发和续期由部署环境负责，当前 Compose 没有 Certbot 服务或自动续期任务。如果使用证书副本，续期后需同步 `ssl/` 中的文件并重载 Nginx；仅更新证书原始目录不会更新副本。

当前 Compose 仍将应用端口直接映射到宿主机。需要只通过代理访问时，应按部署环境调整端口绑定或访问规则。

## 4. 监控、备份与更新

### 状态和日志

```bash
curl http://localhost:8000/health
docker compose ps
docker compose logs --tail=100 gold-monitor
```

`/health` 返回数据库连接状态、`collector_running`、采集统计、最后价格和时间等信息。当前容器 healthcheck 只检查该接口的 HTTP 成功状态；判断数据源是否正常，还应查看 JSON 中的 `data_source_healthy`、连续失败数和最后更新时间。不要把容器运行中等同于上游行情可用。

### SQLite 一致性备份

运行中备份应调用应用备份接口。以下 Bash 示例假设管理员凭证已经设为当前 shell 的 `GOLD_ADMIN_API_KEY` 环境变量；Compose 读取 `.env` 不会自动向 shell 导出该变量。

```bash
curl --fail --request POST \
  --header "X-Admin-Key: ${GOLD_ADMIN_API_KEY}" \
  "http://localhost:8000/api/data/backup?backup_name=manual"
```

确认 JSON 返回 `success=true`，按 `backup_path` 取出**已生成的备份文件**。使用当前默认路径时：

```bash
mkdir -p backup
docker cp gold-monitor:/app/backups/manual.db ./backup/manual.db
```

不要把直接复制运行中的原始 SQLite 文件当成一致性备份，尤其数据库可能使用 WAL。当前备份接口通过 SQLite backup API 生成快照。模型配置文件与主密钥也需要单独保管；仅有数据库文件不足以恢复全部应用配置。

离线恢复方法要求先停止所有数据库使用者并关闭数据库连接，验证文件后恢复，且不提供 HTTP 恢复路由。具体方法及非 SQLite 的 `prices_only` 导出范围见 [验证记录](refactor-validation.md)。

### 更新、端口和持久数据

更新代码后重新构建主服务：

```bash
docker compose up -d --build gold-monitor
```

将 `.env` 中 `PORT` 设置为需要的宿主机端口，例如 `PORT=8080`，再重新创建服务即可；应用在容器内仍监听 8000。

数据库和默认模型配置使用 named volume。普通容器重建会继续使用它；删除 volume 会同时删除其中的数据。当前项目未配置日志文件挂载，日志主要通过容器日志查看；保留时长和轮转由部署环境设置。

## 5. 当前限制

- 本轮没有完成 Docker 引擎可用后的镜像构建、容器启动及完整接口验证，最终状态以 [验证记录](refactor-validation.md) 为准。
- 单进程限制适用于本地与容器运行；当前没有独立后台 worker、分布式调度或通知持久队列。
- 通知结果会入库，但进程异常退出后不保证补发或恰好发送一次。
- 当前仅清理过期原始报价，小时/日聚合未实现；定时清理不是定时备份。需要定时备份时，由部署方安排对备份接口的调用并保存产物。
