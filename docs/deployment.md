# 部署指南

本文档介绍如何将金价监控系统部署到生产环境。

## 目录

1. [Docker 部署](#docker-部署)
2. [HTTPS/SSL 配置](#httpsssl-配置)
3. [环境变量配置](#环境变量配置)
4. [监控与运维](#监控与运维)

---

## Docker 部署

### 快速启动

```bash
# 1. 克隆项目
git clone <repo-url>
cd gold-price-alert

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，配置必要的参数

# 3. 启动服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f gold-monitor
```

### 服务说明

| 服务 | 说明 | 端口 |
|------|------|------|
| `gold-monitor` | 主 Web 服务 | 8000 |
| `gold-collector` | 数据采集服务（可选） | - |
| `nginx` | 反向代理（生产环境） | 80/443 |

### 启动不同配置

```bash
# 仅启动 Web 服务
docker-compose up -d gold-monitor

# 启动 Web + 数据采集
docker-compose --profile collector up -d

# 生产环境（含 Nginx）
docker-compose --profile production up -d
```

---

## HTTPS/SSL 配置

### 方式一：Let's Encrypt 免费证书

```bash
# 1. 安装 certbot
apt-get install certbot

# 2. 获取证书（确保域名已解析到服务器）
certbot certonly --standalone -d your-domain.com

# 3. 证书会保存在 /etc/letsencrypt/live/your-domain.com/

# 4. 复制证书到项目目录
mkdir -p ssl
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ssl/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem ssl/
```

### 方式二：自签名证书（测试用）

```bash
# 生成自签名证书
mkdir -p ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout ssl/privkey.pem \
    -out ssl/fullchain.pem \
    -subj "/CN=localhost"
```

### 配置 Nginx 启用 HTTPS

编辑 `nginx.conf`，取消注释 HTTPS 部分：

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    
    # ... 其他配置
}
```

### 自动续期证书

```bash
# 添加 cron 任务
0 0 1 * * certbot renew --quiet && docker-compose restart nginx
```

---

## 环境变量配置

### 必须配置

| 变量 | 说明 | 示例 |
|------|------|------|
| `GOLD_DATA_SOURCE` | 数据源类型 | `mock`, `sina`, `goldapi` |
| `GOLD_DATABASE_URL` | 数据库连接 | `sqlite:///data/gold.db` |

### 数据源配置

```bash
# 使用 GoldAPI.io
GOLD_DATA_SOURCE=goldapi
GOLD_GOLDAPI_KEY=your-api-key
```

### 大模型配置

```bash
# 使用 Claude
GOLD_LLM_PROVIDER=anthropic
GOLD_ANTHROPIC_API_KEY=sk-ant-xxx

# 或使用 OpenAI
GOLD_LLM_PROVIDER=openai
GOLD_OPENAI_API_KEY=sk-xxx
GOLD_OPENAI_BASE_URL=https://api.openai.com/v1  # 可选，用于兼容接口
```

### 告警通知配置

```bash
# 邮件通知
GOLD_SMTP_HOST=smtp.gmail.com
GOLD_SMTP_PORT=587
GOLD_SMTP_USERNAME=your-email@gmail.com
GOLD_SMTP_PASSWORD=your-app-password
GOLD_ALERT_EMAIL_TO=recipient@example.com

# 钉钉 Webhook
GOLD_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
GOLD_WEBHOOK_TYPE=dingtalk

# Telegram
GOLD_TELEGRAM_BOT_TOKEN=123456:ABC-xxx
GOLD_TELEGRAM_CHAT_ID=123456789
```

---

## 监控与运维

### 健康检查

```bash
# 检查服务状态
curl http://localhost:8000/health

# 响应示例
{
    "status": "healthy",
    "database": "connected",
    "data_source": "mock",
    "data_source_healthy": true,
    "last_price": 2050.50,
    "uptime_seconds": 3600.5
}
```

### 日志查看

```bash
# 实时日志
docker-compose logs -f gold-monitor

# 最近 100 行
docker-compose logs --tail=100 gold-monitor
```

### 数据备份

```bash
# 备份 SQLite 数据库
docker cp gold-monitor:/app/data/gold_prices.db ./backup/

# 或使用 volume 备份
docker run --rm -v gold-price-alert_gold_data:/data -v $(pwd)/backup:/backup \
    alpine tar czf /backup/gold_data_$(date +%Y%m%d).tar.gz /data
```

### 常见问题

#### 1. 端口被占用

```bash
# 修改 docker-compose.yml 中的端口映射
ports:
  - "8080:8000"  # 改为 8080
```

#### 2. 数据源连接失败

检查网络连接和 API Key 配置：

```bash
# 测试 GoldAPI
curl -H "x-access-token: YOUR_API_KEY" https://www.goldapi.io/api/XAU/USD
```

#### 3. 内存不足

```bash
# 限制容器内存
docker-compose up -d --memory=512m gold-monitor
```

---

## 生产环境检查清单

- [ ] 配置正确的数据源（非 mock）
- [ ] 配置大模型 API Key
- [ ] 配置告警通知渠道
- [ ] 启用 HTTPS
- [ ] 配置日志持久化
- [ ] 设置定时备份
- [ ] 配置防火墙规则
- [ ] 测试健康检查接口
