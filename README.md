# 金价实时监控与智能分析系统

实时监控黄金价格，自动告警通知，AI 智能分析市场动态。

**纯 Web 模式** - 一个服务包含所有功能：Web 界面 + 数据采集 + 告警检测 + AI 分析。

## 功能特性

- 🔄 **自动数据采集** - 后台自动采集金价，支持多数据源
- ⚠️ **智能告警** - 阈值告警、波动告警，自动检测
- 🤖 **AI 分析** - 调用大模型分析价格波动原因
- 📊 **可视化展示** - 完整时间窗口走势图、银行参考报价、汇率换算
- 🐳 **Docker 配置** - 提供 Dockerfile 与 Compose 部署入口

## 快速开始

### 方式一：本地运行

```bash
# 1. 安装
pip install -e .

# 2. 启动服务
gold-monitor --host 127.0.0.1

# 3. 访问 http://localhost:8000
```

### 方式二：Docker 运行

先按 [.env.production.example](.env.production.example) 配置管理员 API Key、主密钥及服务参数，再启动：

```bash
# 1. 启动
docker compose up -d

# 2. 查看日志
docker compose logs -f

# 3. 访问 http://localhost:8000
```

本轮重构尚未完成 Docker 构建与运行验收：本机 Docker Desktop 引擎启动失败。此处是项目提供的运行方式，实际验证状态见 [重构验证记录](docs/refactor-validation.md)。

### 运行方式与部署范围

一个应用进程负责 Web、采集、告警和定时分析。当前使用**单进程、单 worker**；不要让多个 worker 或实例同时写同一数据库和模型配置文件。进程内任务、缓存、限流及 WebSocket 连接没有分布式协调。

`gold-monitor` 和 `gold_monitor.web:app` 入口保持可用。代码中可用 `create_app(Settings(...))` 创建独立应用；数据库在 lifespan 内初始化，通过 `app.state.runtime.db` 访问。旧 `web.db` 是仅限默认应用生命周期内使用的弃用兼容入口。

## 配置

复制 `.env.example` 为 `.env` 并编辑：

```bash
# 数据源: mock(模拟), sina(新浪), goldapi(GoldAPI.io)
GOLD_DATA_SOURCE=mock

# 采集间隔（秒）
GOLD_FETCH_INTERVAL=30

# 告警阈值
GOLD_ALERT_THRESHOLD_PERCENT=1.0
GOLD_ALERT_PRICE_UPPER=2500.0
GOLD_ALERT_PRICE_LOWER=1800.0

# 大模型: mock, anthropic, openai
GOLD_LLM_PROVIDER=mock
GOLD_ANTHROPIC_API_KEY=your-key
GOLD_OPENAI_API_KEY=your-key

# 模型配置保存位置
GOLD_LLM_CONFIG_PATH=llm_config.json
```

模型选择优先级为：显式 provider 参数 → 已保存的模型配置文件 → 应用 Settings。没有配置文件时，Settings 根据环境和 `.env` 生成默认配置；无有效密钥时使用 Mock。已有保存配置不会被 `GOLD_LLM_PROVIDER` 自动覆盖。需要重新使用环境默认值时，在设置中重置配置，或指定新的模型配置路径。

通知配置使用当前应用的环境默认值，再应用已保存的渠道启用状态和字段覆盖。保存配置、测试发送、实际告警使用同一解析路径；变更对后续发送生效。启用 `GOLD_ENABLE_AUTH` 后，在页面设置中填写对应管理员凭证，页面统一发送 `X-Admin-Key`，凭证只保留在当前页面内存中。

设置 `GOLD_ENCRYPT_API_KEYS=true` 时，必须配置稳定的 `GOLD_SECRET_KEY` 并在重启后保留同一值，否则已有密文无法读取。模型配置文件损坏、解密失败或写入失败会明确报错。

## 数据含义

- 主图表、告警阈值和本地波动分析以 **USD/oz** 为单位；本地分析和图表先筛选 USD，原始历史仍保留其他币种记录。
- 图表最多返回 2000 个绘图点，覆盖整个所选时间窗口并保留首尾及分桶峰谷；高低价、均价、涨跌幅和 `count` 使用完整窗口数据。`count` 是原始入库样本数，不是绘图点数。
- API、WebSocket 和导出的时间带 UTC `Z`；旧无时区时间按 UTC 解释，界面转换为浏览器本地时间。
- 银行卡片目前使用国际金价/汇率与预设价差生成参考报价，未接入银行真实挂牌接口。Mock 模式下使用模拟数据；汇率接口会返回回退和过期标记。
- 检测到历史间隙时只能补充当前报价，不能还原历史价格。SQLite 备份支持一致性快照；非 SQLite 的 JSON 兼容导出仅包含价格记录。离线恢复要求先关闭数据库，详见 [备份和验证说明](docs/refactor-validation.md)。

## 命令行参数

```bash
gold-monitor [选项]

选项:
  --host TEXT     监听地址 (默认: 0.0.0.0)
  --port INTEGER  监听端口 (默认: 8000)
  --reload        开发模式，自动重载
  --version       显示版本号
```

## API 接口

| 接口 | 说明 |
|------|------|
| `/` | Web 界面 |
| `/health` | 健康检查（含采集状态） |
| `/api/price/current` | 获取当前金价 |
| `/api/price/history` | 历史价格 |
| `/api/chart/data` | 图表数据 |
| `/api/alerts` | 告警历史 |
| `/api/analysis` | AI 分析 |
| `/api/bank-prices` | 银行金价 |
| `/api/exchange-rate` | 汇率 |
| `/api/convert` | 价格换算 |
| `/docs` | API 文档 |

## 项目结构

```
gold-price-alert/
├── src/gold_monitor/
│   ├── web.py             # FastAPI 工厂和兼容 ASGI 入口
│   ├── runtime.py         # 应用实例服务装配与生命周期
│   ├── dependencies.py    # 从请求获取当前实例
│   ├── cli.py             # 命令行入口
│   ├── config.py          # Settings
│   ├── models.py          # ORM 表与 Database 兼容导出
│   ├── storage/           # 会话、事务、查询和图表聚合
│   ├── collector.py       # 报价获取与保存编排
│   ├── collection/        # 采集策略、统计与调度
│   ├── data_sources/      # 行情适配器与工厂
│   ├── alerts/            # 告警规则与数据对象
│   ├── notifications/     # 发送渠道与重试
│   ├── analysis/          # 供应商、提示词、解析和配置工厂
│   ├── services/          # API 使用的业务服务
│   ├── routers/           # HTTP/WebSocket 入口
│   ├── llm_config.py      # 模型配置文件读写
│   ├── data_lifecycle.py  # 清理、导出、备份和离线恢复
│   ├── time_utils.py      # UTC 时间约定
│   └── static/            # HTML、JS modules 与 CSS
├── tests/                 # 后端与前端行为测试
├── docs/                  # 契约、计划、验证及部署文档
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## 技术栈

- **后端**: Python 3.10+, FastAPI, SQLAlchemy
- **前端**: ECharts, 原生 JavaScript
- **数据库**: SQLite
- **大模型**: Claude / OpenAI / Mock

## 项目文档

- [需求说明](docs/requirements.md)
- [部署指南](docs/deployment.md)
- [API 契约与兼容字段](docs/api-contract.md)
- [重构计划与验收清单](docs/refactoring-plan.md)
- [本轮交付、验证结果与限制](docs/refactor-validation.md)

## License

MIT
