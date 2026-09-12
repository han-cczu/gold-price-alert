# 部署指南

本文按当前 [docker-compose.yml](../docker-compose.yml)、[Dockerfile](../Dockerfile) 和 [CLI](../src/gold_monitor/cli.py) 编写。应用是一个内建采集、告警和定时分析的 Web 服务。

基线 `05802d0` 已通过 [GitHub CI 的镜像构建和隔离 Mock 容器运行检查](https://github.com/han-cczu/gold-price-alert/actions/runs/34442124715)。本机 Docker Desktop 引擎仍不可用；本次可靠性修复的验证结果单独记录在 [修复与验证记录](reliability-fixes.md)，不沿用基线结果作为新代码的容器验收。

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
| `GOLD_TRUSTED_PROXY_IPS` | CLI 启动时交给 Uvicorn 的可信代理 IP/CIDR；Compose 默认 `127.0.0.1,172.28.0.10`，后者是自带 Nginx 容器的固定地址 |

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
| 手动及自动备份 | `/app/data/backups/`；自动快照位于 `automatic/` 子目录 | 包含在 `gold_data` 中；仍应定期复制到独立存储 |
| Nginx 配置 | `/etc/nginx/nginx.conf` | 从项目 `nginx.conf` 只读挂载 |
| TLS 证书 | `/etc/nginx/ssl/` | 从项目 `ssl/` 只读挂载 |

Compose 明确设置 `GOLD_DATABASE_URL=sqlite:///data/gold_prices.db`。仅修改宿主机 `.env` 中同名值不会替换这一固定项；本地 Python 运行才会直接使用本地 Settings 读取的数据库 URL。生产模板中的其他数据库示例不表示当前 Compose 已配置或验证该数据库。

Compose 的 `.env` 用于替换文件中明确引用的 `${...}`，不会把所有 `GOLD_*` 自动注入容器。现已透传数据保留天数、同价采样间隔、自动备份开关、备份周期和保留份数；备份路径固定为 `/app/data/backups`，需要改变时请同步调整 Compose 环境项与持久目录挂载。

从旧版升级前，先导出旧容器 `/app/backups/` 中已有快照；新默认路径不会迁移旧文件，重建旧容器会丢失未导出的容器内文件。

### 行情、分析和通知

- `GOLD_DATA_SOURCE` 支持 `mock`、`sina`、`goldapi`、`fallback`；GoldAPI 使用 `GOLD_GOLDAPI_KEY`，采集间隔使用 `GOLD_FETCH_INTERVAL`。Mock 为模拟数据，不是实时行情。
- `fallback` 仅在真实数据源间切换，每次按 GoldAPI（有密钥时）、Sina 的优先级尝试；每源默认最多 10 秒，主源恢复后重新使用主源。全部失败时明确报不可用，不自动改用 Mock。
- 同价去重默认每 300 秒保存一个真实采样，通过 `GOLD_PRICE_HEARTBEAT_SECONDS` 调整；不会生成虚构的历史点。
- 首次没有模型配置文件时，Settings 根据 `GOLD_LLM_PROVIDER`、`GOLD_ANTHROPIC_API_KEY`、`GOLD_OPENAI_API_KEY` 等初始化默认配置；无有效密钥时使用 Mock。DeepSeek 端点未选择模型时默认 `deepseek-v4-flash`。
- 启动时先从分析历史恢复 24 小时内同一供应商与模型的智能分析结果；没有可复用结果才调用模型，避免每次重启都产生付费调用。
- 已保存模型文件的优先级高于 Settings 默认值。修改环境变量不会自动改写已保存的平台选择；可在页面切换/重置模型配置。代码中显式传入 provider 时，显式选择优先。
- `GOLD_OPENAI_BASE_URL` 配置兼容端点；`GOLD_TAVILY_API_KEY` 用于已实现的搜索路径。模型是否真正搜索以响应中的标记和来源为准。
- 邮件使用 `GOLD_SMTP_*` 与 `GOLD_ALERT_EMAIL_TO`；Webhook 使用 `GOLD_WEBHOOK_URL/GOLD_WEBHOOK_TYPE`；Telegram 使用 `GOLD_TELEGRAM_BOT_TOKEN/GOLD_TELEGRAM_CHAT_ID`。上述项已在当前 Compose 中透传。
- 通知以 Settings 为默认值，再应用数据库保存的启用状态和字段覆盖。页面保存、测试发送和实际告警使用同一解析路径；启用某渠道后再用测试功能检查该部署环境下的真实连通性。
- 邮件在端口 465 使用隐式 TLS，其余端口要求 STARTTLS。钉钉、企业微信 Webhook 只有响应体 `errcode` 为 0 才记为发送成功，HTTP 200 但机器人拒绝的情况会记入失败日志。
- 启用鉴权后，新建本地/智能分析需要管理员凭证；已有智能分析缓存可公开读取。成功结果保存到分析历史，共享的智能分析任务只保存一份记录。

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

限流不直接相信客户端的 `X-Forwarded-For`。经代理访问时，将 `GOLD_TRUSTED_PROXY_IPS` 设置为实际代理的地址或受控 CIDR，由 Uvicorn 验证代理并解析客户端地址；不要对仍允许公网直连的应用设置 `*`。Compose 给 Nginx 容器分配固定地址 `172.28.0.10`（子网 `172.28.0.0/24`），并默认把它与回环地址一起列入可信代理，经自带 Nginx 访问的每个客户端各有独立的限流身份。若该子网与宿主机现有网络冲突，请同时修改 `docker-compose.yml` 中的子网、Nginx 地址和 `.env` 里的 `GOLD_TRUSTED_PROXY_IPS`。使用独立 `uvicorn gold_monitor.web:app` 命令时，改用 Uvicorn 的 `--forwarded-allow-ips` 参数或进程环境变量 `FORWARDED_ALLOW_IPS`。

## 4. 监控、备份与更新

### 状态和日志

```bash
curl http://localhost:8000/health
docker compose ps
docker compose logs --tail=100 gold-monitor
```

`/health` 返回数据库连接状态、`collector_running`、采集统计、最后价格和时间。仅数据库连通、采集任务运行、最近采样成功且未超过 `max(30秒, 3 × 当前采集间隔)` 时返回 HTTP 200，其余返回 503。容器通过 `python -m gold_monitor.healthcheck` 同时检查 HTTP 和健康 JSON。数据源故障会使容器标记为 unhealthy；Docker 的 `restart: unless-stopped` 本身不会因 unhealthy 自动重启容器。

`/api/price/current` 返回采集器最近的成功报价，不再每次请求都访问上游行情源；报价过期时最多每个采集间隔重新抓取一次。所有 `/api/` 路径按客户端限流（默认每分钟 60 次，`/health` 与静态资源除外），多个访客共用一个出口地址时可调高 `GOLD_RATE_LIMIT_PER_MINUTE`。

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
docker cp gold-monitor:/app/data/backups/manual.db ./backup/manual.db
```

不要把直接复制运行中的原始 SQLite 文件当成一致性备份，尤其数据库可能使用 WAL。当前备份接口通过 SQLite backup API 生成快照。模型配置文件与主密钥也需要单独保管；仅有数据库文件不足以恢复全部应用配置。

离线恢复方法要求先停止所有数据库使用者并关闭数据库连接，验证文件后恢复，且不提供 HTTP 恢复路由。具体方法及非 SQLite 的 `prices_only` 导出范围见 [验证记录](refactor-validation.md)。

### 自动快照

`GOLD_BACKUP_ENABLED=true` 后，应用启动时执行一次快照，此后每 `GOLD_BACKUP_INTERVAL_HOURS` 小时执行（默认 24）。`GOLD_BACKUP_KEEP_COUNT` 默认 7，仅轮转 `automatic/` 子目录内应用生成的快照；新备份失败时保留全部旧快照，手动备份不参与轮转。生产模板开启自动备份，未使用模板时应用默认关闭。

自动快照与手动快照均使用已有数据库一致性备份实现。数据库快照不包含模型配置文件或主密钥，不能替代完整应用备份；同一个数据卷内的快照也不能防止整卷丢失。

### 更新、端口和持久数据

更新代码后重新构建主服务：

```bash
docker compose up -d --build gold-monitor
```

新版本启动时会为已有 SQLite 数据库补建缺失的索引（时间戳、币种加时间戳、告警与通知日志时间）；在大表上首次启动可能多花几秒，之后正常。

将 `.env` 中 `PORT` 设置为需要的宿主机端口，例如 `PORT=8080`，再重新创建服务即可；应用在容器内仍监听 8000。

数据库和默认模型配置使用 named volume。普通容器重建会继续使用它；删除 volume 会同时删除其中的数据。当前项目未配置日志文件挂载，日志主要通过容器日志查看；保留时长和轮转由部署环境设置。

## 5. 当前限制

- 基线的隔离 Mock 容器检查已通过；本次修复及真实服务验证范围见 [修复记录](reliability-fixes.md)。
- 单进程限制适用于本地与容器运行；当前没有独立后台 worker、分布式调度或通知持久队列。
- 通知结果会入库，但进程异常退出后不保证补发或恰好发送一次。
- 当前仅清理过期原始报价（启动时执行一次，此后每 24 小时），小时/日聚合未实现，对应两个配置项仅兼容保留；自动备份使用独立调度，不依赖清理任务。
